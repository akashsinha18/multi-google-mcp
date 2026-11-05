"""Google Drive tool implementations."""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any

from googleapiclient.http import MediaInMemoryUpload

from .context import AppContext
from .fanout import decode_page_tokens, encode_page_tokens, fan_out, merge_sorted
from .store import Account
from .text import html_to_text, normalize_whitespace, truncate

log = logging.getLogger(__name__)

MAX_RESULTS_CAP = 100
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_CHARS = 20_000
SHEET_ROW_CAP = 200

GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
GSLIDES = "application/vnd.google-apps.presentation"
GFOLDER = "application/vnd.google-apps.folder"
PDF = "application/pdf"

TEXTLIKE_MIME = {
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/yaml", "application/x-sh", "application/sql",
}

FILE_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners(emailAddress),driveId,parents"
LIST_FIELDS = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"


class DriveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------
def escape_drive_string(value: str) -> str:
    """Escape a value for use inside single quotes in a Drive `q` string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


_TERM_RE = re.compile(r'"([^"]+)"|(\S+)')


def build_search_query(keywords: str) -> str:
    """Turn plain keywords into a safe Drive query.

    Every term (or "quoted phrase") must match the name or full text. All user
    input is escaped, so it can never alter the query structure.
    """
    terms = [m.group(1) or m.group(2) for m in _TERM_RE.finditer(keywords or "")]
    terms = [t.strip() for t in terms if t and t.strip()]
    clauses = []
    for t in terms[:10]:
        e = escape_drive_string(t)
        clauses.append(f"(name contains '{e}' or fullText contains '{e}')")
    clauses.append("trashed = false")
    return " and ".join(clauses)


def _clamp(n: int) -> int:
    return max(1, min(MAX_RESULTS_CAP, int(n)))


def _compact_file(f: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": f.get("id"),
        "name": f.get("name"),
        "mime_type": f.get("mimeType"),
        "modified": f.get("modifiedTime"),
        "link": f.get("webViewLink"),
    }
    if f.get("size") is not None:
        out["size"] = int(f["size"])
    owners = [o.get("emailAddress") for o in f.get("owners", []) or [] if o.get("emailAddress")]
    if owners:
        out["owners"] = owners
    if f.get("driveId"):
        out["shared_drive_id"] = f["driveId"]
    return out


def _list_files(service: Any, q: str, page_size: int, page_token: str | None, order_by: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "q": q,
        "pageSize": page_size,
        "fields": LIST_FIELDS,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "allDrives",
    }
    if order_by:
        params["orderBy"] = order_by
    if page_token:
        params["pageToken"] = page_token
    res = service.files().list(**params).execute()
    return {
        "files": [_compact_file(f) for f in res.get("files", []) or []],
        "next_page_token": res.get("nextPageToken"),
        "incomplete": bool(res.get("incompleteSearch")),
    }


async def _fan_list(
    ctx: AppContext,
    account: str,
    q: str,
    max_results: int,
    page_token: str | None,
    order_by: str | None,
    extra: dict[str, Any],
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
        return await ctx.google.run(a, "drive", lambda s: _list_files(s, q, max_results, tok, order_by))

    if not multi:
        a = accounts[0]
        r = await one(a)
        result = {**extra, "account": a.ref, "count": len(r["files"]), "files": r["files"],
                  "next_page_token": r["next_page_token"]}
        if r["incomplete"]:
            result["incomplete_search"] = True
        return result

    ok, errors = await fan_out(accounts, one)
    files = merge_sorted([(a, r["files"]) for a, r in ok], sort_key="modified")
    result = {
        **extra,
        "accounts": [a.ref for a in accounts],
        "count": len(files),
        "files": files,
        "next_page_token": encode_page_tokens({a.alias: r["next_page_token"] for a, r in ok}),
    }
    if any(r["incomplete"] for _, r in ok):
        result["incomplete_search"] = True
    if errors:
        result["errors"] = errors
    return result


async def search(ctx: AppContext, query: str, account: str = "all", max_results: int = 20,
                 page_token: str | None = None) -> dict[str, Any]:
    q = build_search_query(query)
    # Drive does not support orderBy together with fullText; results are relevance-ranked
    # per account and merged by modified time.
    return await _fan_list(ctx, account, q, max_results, page_token, None, {"query": query})


async def list_recent(ctx: AppContext, account: str = "all", max_results: int = 20,
                      page_token: str | None = None) -> dict[str, Any]:
    q = f"trashed = false and mimeType != '{GFOLDER}'"
    return await _fan_list(ctx, account, q, max_results, page_token, "modifiedTime desc", {})


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def pdf_to_text(data: bytes, max_chars: int) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        t = page.extract_text() or ""
        parts.append(t)
        total += len(t)
        if total > max_chars * 2:  # stop early; we'll truncate anyway
            break
    return normalize_whitespace("\n\n".join(parts)), len(reader.pages)


def _a1_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def sheets_to_csv(sheets_service: Any, file_id: str, row_cap: int) -> list[dict[str, Any]]:
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=file_id, fields="sheets.properties(title,sheetId,gridProperties)"
    ).execute()
    props = [s["properties"] for s in meta.get("sheets", []) or []]
    if not props:
        return []
    ranges = [f"{_a1_sheet(p['title'])}!1:{row_cap}" for p in props]
    vals = sheets_service.spreadsheets().values().batchGet(
        spreadsheetId=file_id, ranges=ranges, valueRenderOption="FORMATTED_VALUE"
    ).execute()
    out = []
    for p, vr in zip(props, vals.get("valueRanges", []) or []):
        rows = vr.get("values", []) or []
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        grid_rows = (p.get("gridProperties") or {}).get("rowCount", len(rows))
        out.append(
            {
                "sheet": p["title"],
                "rows_returned": len(rows),
                "rows_truncated": len(rows) >= row_cap and grid_rows > row_cap,
                "csv": buf.getvalue(),
            }
        )
    return out


async def read_file(ctx: AppContext, file_id: str, account: str, max_chars: int = DEFAULT_MAX_CHARS,
                    sheet_row_cap: int = SHEET_ROW_CAP) -> dict[str, Any]:
    a = ctx.resolve_one(account)
    max_chars = max(500, min(int(max_chars), 200_000))
    sheet_row_cap = max(1, min(int(sheet_row_cap), 5000))

    meta = await ctx.google.run(
        a, "drive",
        lambda s: s.files().get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True).execute(),
    )
    info = _compact_file(meta)
    mime = meta.get("mimeType", "")
    base = {"account": a.ref, "file": info}

    if mime == GSHEET:
        sheets = await ctx.google.run(a, "sheets", lambda s: sheets_to_csv(s, file_id, sheet_row_cap))
        budget = max_chars
        truncated = False
        out_sheets: list[dict[str, Any]] = []
        for sh in sheets:
            if budget <= 0:
                truncated = True
                out_sheets.append({"sheet": sh["sheet"], "omitted": True})
                continue
            text, cut = truncate(sh["csv"], budget)
            sh["csv"] = text
            if cut:
                sh["csv_truncated"] = True
                truncated = True
            if sh["rows_truncated"]:
                truncated = True
            budget -= len(text)
            out_sheets.append(sh)
        return {**base, "format": "csv", "row_cap": sheet_row_cap, "sheets": out_sheets,
                "truncated": truncated}

    if mime in (GDOC, GSLIDES):
        export_mime = "text/markdown" if mime == GDOC else "text/plain"

        def export(s: Any) -> bytes:
            from googleapiclient.errors import HttpError
            try:
                return s.files().export(fileId=file_id, mimeType=export_mime).execute()
            except HttpError as e:
                if export_mime != "text/plain" and getattr(e.resp, "status", None) == 400:
                    return s.files().export(fileId=file_id, mimeType="text/plain").execute()
                raise

        text = normalize_whitespace(_decode_text(await ctx.google.run(a, "drive", export)))
        fmt = "markdown" if mime == GDOC else "text"
        return {**base, "format": fmt, **_content(text, max_chars)}

    size = info.get("size") or 0
    is_text = mime.startswith("text/") or mime in TEXTLIKE_MIME
    if mime == PDF or is_text:
        if size > MAX_DOWNLOAD_BYTES:
            return {**base, "supported": False,
                    "reason": f"file is {size} bytes; limit is {MAX_DOWNLOAD_BYTES}"}
        data: bytes = await ctx.google.run(
            a, "drive", lambda s: s.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        )
        if mime == PDF:
            text, pages = pdf_to_text(data, max_chars)
            res = {**base, "format": "text", "pages": pages, **_content(text, max_chars)}
            if not text:
                res["note"] = "No extractable text (the PDF may be scanned images)."
            return res
        text = _decode_text(data)
        if mime == "text/html":
            text = html_to_text(text)
        return {**base, "format": "text", **_content(text, max_chars)}

    return {**base, "supported": False,
            "reason": f"Reading files of type {mime or 'unknown'} is not supported; metadata only."}


def _content(text: str, max_chars: int) -> dict[str, Any]:
    total = len(text)
    text, cut = truncate(text, max_chars)
    out: dict[str, Any] = {"content": text, "truncated": cut}
    if cut:
        out["total_chars"] = total
    return out


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
# Upload mime -> (media mime, target Drive mime). Google types convert on import.
_CONVERT = {
    GDOC: "text/plain",
    GSHEET: "text/csv",
}


async def create_file(ctx: AppContext, account: str, name: str, content: str,
                      mime_type: str = "text/plain", folder_id: str | None = None) -> dict[str, Any]:
    a = ctx.resolve_one(account)
    if not name.strip():
        raise DriveError("name must not be empty")
    media_mime = _CONVERT.get(mime_type, mime_type)
    if not (media_mime.startswith("text/") or media_mime in TEXTLIKE_MIME):
        raise DriveError(
            f"Unsupported mime_type {mime_type!r}. Use a text type (text/plain, text/markdown, "
            f"text/csv, text/html, application/json) or {GDOC} / {GSHEET} to convert."
        )
    body: dict[str, Any] = {"name": name}
    if mime_type in _CONVERT:
        body["mimeType"] = mime_type
    if folder_id:
        body["parents"] = [folder_id]
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=media_mime, resumable=False)

    def fn(s: Any) -> dict[str, Any]:
        return s.files().create(body=body, media_body=media, fields=FILE_FIELDS,
                                supportsAllDrives=True).execute()

    f = await ctx.google.run(a, "drive", fn)
    return {"account": a.ref, "status": "created", "file": _compact_file(f)}
