"""Gmail tool implementations (compact results, no raw API payloads)."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

from .context import AppContext
from .fanout import decode_page_tokens, encode_page_tokens, fan_out, merge_sorted
from .store import Account
from .text import html_to_text, normalize_whitespace, truncate

SEARCH_HEADERS = ["From", "To", "Subject", "Date"]
THREAD_HEADERS = {"from", "to", "cc", "bcc", "date", "subject", "reply-to"}
MAX_RESULTS_CAP = 100
SYSTEM_LABELS = {
    "INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "SENT", "DRAFT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def _clamp(n: int, lo: int = 1, hi: int = MAX_RESULTS_CAP) -> int:
    return max(lo, min(hi, int(n)))


def _iso_from_ms(ms: str | int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _headers(payload: dict[str, Any], wanted: set[str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = h.get("name", "")
        key = name.lower()
        if wanted is None or key in wanted:
            out.setdefault(key, h.get("value", ""))
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _search_one(service: Any, query: str, max_results: int, page_token: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"userId": "me", "q": query, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token
    listing = service.users().messages().list(**params).execute()
    ids = [m["id"] for m in listing.get("messages", []) or []]

    fetched: dict[str, dict[str, Any]] = {}
    failed: list[str] = []

    def _cb(request_id: str, response: Any, exception: Exception | None) -> None:
        if exception is not None:
            failed.append(request_id)
        else:
            fetched[request_id] = response

    if ids:
        batch = service.new_batch_http_request(callback=_cb)
        for mid in ids:
            batch.add(
                service.users().messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=SEARCH_HEADERS
                ),
                request_id=mid,
            )
        batch.execute()

    items = []
    for mid in ids:
        msg = fetched.get(mid)
        if msg is None:
            continue
        h = _headers(msg.get("payload", {}))
        labels = msg.get("labelIds", []) or []
        items.append(
            {
                "id": msg["id"],
                "thread_id": msg.get("threadId"),
                "date": _iso_from_ms(msg.get("internalDate")),
                "from": h.get("from"),
                "to": h.get("to"),
                "subject": h.get("subject"),
                "snippet": msg.get("snippet"),
                "unread": "UNREAD" in labels,
                "labels": [l for l in labels if l != "UNREAD"],
            }
        )
    return {"messages": items, "next_page_token": listing.get("nextPageToken"), "failed": len(failed)}


async def search(
    ctx: AppContext,
    query: str,
    account: str = "all",
    max_results: int = 20,
    page_token: str | None = None,
) -> dict[str, Any]:
    max_results = _clamp(max_results)
    accounts = ctx.resolve(account, allow_all=True)
    multi = len(accounts) > 1 or account.strip().lower() == "all"

    tokens: dict[str, str] = {}
    if page_token and multi:
        tokens = decode_page_tokens(page_token)
        accounts = [a for a in accounts if a.alias in tokens]

    async def one(a: Account) -> dict[str, Any]:
        tok = tokens.get(a.alias) if multi else page_token
        return await ctx.google.run(a, "gmail", lambda s: _search_one(s, query, max_results, tok))

    ok, errors = await fan_out(accounts, one)
    messages = merge_sorted([(a, r["messages"]) for a, r in ok], sort_key="date")
    for a, r in ok:
        if r["failed"]:
            errors.append({"account": a.ref, "error": f"{r['failed']} message(s) could not be fetched"})

    if multi:
        next_token = encode_page_tokens({a.alias: r["next_page_token"] for a, r in ok})
    else:
        next_token = ok[0][1]["next_page_token"] if ok else None

    result: dict[str, Any] = {"query": query, "count": len(messages), "messages": messages}
    if not multi and ok:
        result["account"] = ok[0][0].ref
    else:
        result["accounts"] = [a.ref for a in accounts]
    result["next_page_token"] = next_token
    if errors:
        result["errors"] = errors
    if not ok and errors and not multi:
        # Single-account call that failed: surface as a tool error upstream.
        raise _first_error(errors)
    return result


class GmailError(Exception):
    pass


def _first_error(errors: list[dict[str, Any]]) -> GmailError:
    return GmailError(errors[0]["error"])


# ---------------------------------------------------------------------------
# Thread
# ---------------------------------------------------------------------------
def _decode_b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


_CHARSET_RE = re.compile(r'charset="?([\w.:-]+)"?', re.I)


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = _decode_b64(data)
    ctype = _headers(part, {"content-type"}).get("content-type", "")
    m = _CHARSET_RE.search(ctype)
    charset = m.group(1) if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def extract_body_and_attachments(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        disposition = _headers(part, {"content-disposition"}).get("content-disposition", "").lower()
        if filename or body.get("attachmentId") and not mime.startswith("text/"):
            attachments.append(
                {
                    "filename": filename or "(unnamed)",
                    "mime_type": mime,
                    "size": body.get("size", 0),
                    "attachment_id": body.get("attachmentId"),
                }
            )
            continue
        if "attachment" in disposition:
            continue
        if mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            html.append(_part_text(part))
    if any(p.strip() for p in plain):
        text = normalize_whitespace("\n\n".join(plain))
    elif html:
        text = html_to_text("\n".join(html))
    else:
        text = ""
    return text, attachments


def _addresses(value: str | None) -> list[str]:
    if not value:
        return []
    return [f"{n} <{e}>" if n else e for n, e in getaddresses([value]) if e]


def parse_message(msg: dict[str, Any], max_body_chars: int) -> dict[str, Any]:
    payload = msg.get("payload", {}) or {}
    h = _headers(payload, THREAD_HEADERS)
    body, attachments = extract_body_and_attachments(payload)
    total = len(body)
    body, truncated = truncate(body, max_body_chars)
    labels = msg.get("labelIds", []) or []
    out: dict[str, Any] = {
        "id": msg.get("id"),
        "date": _iso_from_ms(msg.get("internalDate")),
        "from": h.get("from"),
        "to": _addresses(h.get("to")),
        "subject": h.get("subject"),
        "unread": "UNREAD" in labels,
        "labels": [l for l in labels if l != "UNREAD"],
        "body": body,
        "body_truncated": truncated,
    }
    if h.get("cc"):
        out["cc"] = _addresses(h.get("cc"))
    if truncated:
        out["body_total_chars"] = total
    if attachments:
        out["attachments"] = [{k: v for k, v in a.items() if k != "attachment_id"} for a in attachments]
    return out


async def get_thread(ctx: AppContext, thread_id: str, account: str, max_body_chars: int = 8000) -> dict[str, Any]:
    a = ctx.resolve_one(account)
    max_body_chars = max(200, min(int(max_body_chars), 100_000))

    def fn(s: Any) -> dict[str, Any]:
        return s.users().threads().get(userId="me", id=thread_id, format="full").execute()

    thread = await ctx.google.run(a, "gmail", fn)
    messages = [parse_message(m, max_body_chars) for m in thread.get("messages", []) or []]
    return {
        "account": a.ref,
        "thread_id": thread.get("id", thread_id),
        "subject": messages[0]["subject"] if messages else None,
        "message_count": len(messages),
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def _labels(service: Any) -> list[dict[str, Any]]:
    return service.users().labels().list(userId="me").execute().get("labels", []) or []


async def list_labels(ctx: AppContext, account: str) -> dict[str, Any]:
    a = ctx.resolve_one(account)
    labels = await ctx.google.run(a, "gmail", _labels)
    return {
        "account": a.ref,
        "labels": sorted(
            ({"id": l["id"], "name": l.get("name"), "type": l.get("type")} for l in labels),
            key=lambda l: (l["type"] != "system", (l["name"] or "").lower()),
        ),
    }


def resolve_label_ids(names_or_ids: list[str], labels: list[dict[str, Any]]) -> list[str]:
    by_id = {l["id"]: l["id"] for l in labels}
    by_name = {(l.get("name") or "").lower(): l["id"] for l in labels}
    out: list[str] = []
    unknown: list[str] = []
    for item in names_or_ids:
        key = item.strip()
        if key in by_id:
            out.append(by_id[key])
        elif key.upper() in SYSTEM_LABELS:
            out.append(key.upper())
        elif key.lower() in by_name:
            out.append(by_name[key.lower()])
        else:
            unknown.append(item)
    if unknown:
        raise GmailError(f"Unknown label(s): {', '.join(unknown)}. Use gmail_list_labels to see available labels.")
    return out


async def modify_labels(
    ctx: AppContext,
    message_or_thread_id: str,
    account: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    target: str = "auto",
) -> dict[str, Any]:
    from googleapiclient.errors import HttpError

    add, remove = list(add or []), list(remove or [])
    if not add and not remove:
        raise GmailError("Nothing to do: provide labels in `add` and/or `remove`.")
    if target not in {"auto", "thread", "message"}:
        raise GmailError("target must be 'auto', 'thread' or 'message'")
    a = ctx.resolve_one(account)

    def fn(s: Any) -> dict[str, Any]:
        labels = _labels(s)
        body = {
            "addLabelIds": resolve_label_ids(add, labels),
            "removeLabelIds": resolve_label_ids(remove, labels),
        }
        id_to_name = {l["id"]: l.get("name") for l in labels}
        used = target
        if target in {"auto", "thread"}:
            try:
                res = s.users().threads().modify(userId="me", id=message_or_thread_id, body=body).execute()
                used = "thread"
                msgs = res.get("messages", []) or []
                label_ids = sorted({lid for m in msgs for lid in (m.get("labelIds") or [])})
            except HttpError as e:
                if target == "thread" or getattr(e.resp, "status", None) not in (400, 404):
                    raise
                used = "message"
        if used == "message":
            res = s.users().messages().modify(userId="me", id=message_or_thread_id, body=body).execute()
            label_ids = sorted(res.get("labelIds", []) or [])
        return {
            "id": message_or_thread_id,
            "target": used,
            "added": [id_to_name.get(i, i) for i in body["addLabelIds"]],
            "removed": [id_to_name.get(i, i) for i in body["removeLabelIds"]],
            "labels_now": [id_to_name.get(i, i) for i in label_ids],
        }

    result = await ctx.google.run(a, "gmail", fn)
    return {"account": a.ref, **result}


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------
def _as_list(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [v.strip() for v in value if v and v.strip()]


def build_mime(
    sender: str | None,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str],
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def create_draft(
    ctx: AppContext,
    account: str,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    reply_to_thread_id: str | None = None,
) -> dict[str, Any]:
    a = ctx.resolve_one(account)
    to_list, cc_list = _as_list(to), _as_list(cc)
    if not to_list:
        raise GmailError("`to` must contain at least one recipient")

    def fn(s: Any) -> dict[str, Any]:
        in_reply_to = references = None
        subj = subject
        if reply_to_thread_id:
            thread = s.users().threads().get(
                userId="me", id=reply_to_thread_id, format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"],
            ).execute()
            msgs = thread.get("messages", []) or []
            if msgs:
                h = _headers(msgs[-1].get("payload", {}), {"message-id", "references", "subject"})
                in_reply_to = h.get("message-id")
                references = h.get("references")
                if not subj:
                    subj = h.get("subject", "")
                if subj and not subj.lower().startswith("re:"):
                    subj = "Re: " + subj
        raw = build_mime(a.email, to_list, subj, body, cc_list, in_reply_to, references)
        message: dict[str, Any] = {"raw": raw}
        if reply_to_thread_id:
            message["threadId"] = reply_to_thread_id
        d = s.users().drafts().create(userId="me", body={"message": message}).execute()
        m = d.get("message", {}) or {}
        return {"draft_id": d.get("id"), "message_id": m.get("id"), "thread_id": m.get("threadId"), "subject": subj}

    res = await ctx.google.run(a, "gmail", fn)
    return {"account": a.ref, **res, "to": to_list, "cc": cc_list, "status": "draft_created"}


async def send_draft(ctx: AppContext, draft_id: str, account: str) -> dict[str, Any]:
    a = ctx.resolve_one(account)

    def fn(s: Any) -> dict[str, Any]:
        m = s.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return {"message_id": m.get("id"), "thread_id": m.get("threadId"), "labels": m.get("labelIds", [])}

    res = await ctx.google.run(a, "gmail", fn)
    return {"account": a.ref, "draft_id": draft_id, **res, "status": "sent"}
