"""Hand-written fakes for Google API service objects (no network)."""

from __future__ import annotations

import base64
from typing import Any, Callable

from multi_google_mcp.store import Account


class Req:
    def __init__(self, fn: Callable[[], Any]):
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


class FakeBatch:
    def __init__(self, callback):
        self._callback = callback
        self._items: list[tuple[str, Req]] = []

    def add(self, request: Req, request_id: str) -> None:
        self._items.append((request_id, request))

    def execute(self) -> None:
        for rid, req in self._items:
            try:
                self._callback(rid, req.execute(), None)
            except Exception as e:  # noqa: BLE001
                self._callback(rid, None, e)


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmail_message(mid: str, ts_ms: int, subject: str, sender: str = "a@x.com", thread_id: str | None = None,
                  labels: list[str] | None = None, payload: dict | None = None) -> dict[str, Any]:
    return {
        "id": mid,
        "threadId": thread_id or mid,
        "internalDate": str(ts_ms),
        "snippet": f"snippet {subject}",
        "labelIds": labels or ["INBOX"],
        "payload": payload or {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@x.com"},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": b64(f"body of {subject}")},
        },
    }


class FakeGmail:
    def __init__(self, messages: list[dict[str, Any]], page_size_token: str | None = None,
                 labels: list[dict[str, Any]] | None = None):
        self.messages_data = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]
        self.next_token = page_size_token
        self.list_calls: list[dict[str, Any]] = []
        self.labels_data = labels or [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "Label_1", "name": "Receipts", "type": "user"},
        ]
        self.modify_calls: list[tuple[str, str, dict]] = []
        self.drafts_created: list[dict] = []
        self.threads_data: dict[str, dict] = {}

    # resource chain
    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    def threads(self):
        return _Threads(self)

    def labels(self):
        return _Labels(self)

    def drafts(self):
        return _Drafts(self)

    def new_batch_http_request(self, callback):
        return FakeBatch(callback)


class _Messages:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, **kw):
        self.g.list_calls.append(kw)
        ids = self.g.order[: kw.get("maxResults", 100)]
        return Req(lambda: {"messages": [{"id": i} for i in ids], "nextPageToken": self.g.next_token})

    def get(self, userId, id, **kw):  # noqa: A002
        return Req(lambda: self.g.messages_data[id])

    def modify(self, userId, id, body):  # noqa: A002
        self.g.modify_calls.append(("message", id, body))
        return Req(lambda: {"id": id, "labelIds": body["addLabelIds"]})


class _Threads:
    def __init__(self, g: FakeGmail):
        self.g = g

    def get(self, userId, id, **kw):  # noqa: A002
        return Req(lambda: self.g.threads_data[id])

    def modify(self, userId, id, body):  # noqa: A002
        from googleapiclient.errors import HttpError

        def run():
            if id not in self.g.threads_data:
                raise HttpError(_Resp(404), b'{"error": {"message": "Requested entity was not found."}}')
            self.g.modify_calls.append(("thread", id, body))
            return {"id": id, "messages": [{"labelIds": body["addLabelIds"]}]}

        return Req(run)


class _Labels:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, userId):
        return Req(lambda: {"labels": self.g.labels_data})


class _Drafts:
    def __init__(self, g: FakeGmail):
        self.g = g

    def create(self, userId, body):
        self.g.drafts_created.append(body)
        return Req(lambda: {"id": "d1", "message": {"id": "m-new", "threadId": body["message"].get("threadId", "t-new")}})

    def send(self, userId, body):
        return Req(lambda: {"id": "sent1", "threadId": "t1", "labelIds": ["SENT"]})


class _Resp(dict):
    def __init__(self, status: int):
        super().__init__()
        self.status = status
        self.reason = "error"


class FakeDrive:
    def __init__(self, files: list[dict[str, Any]] | None = None, exports: dict[str, bytes] | None = None,
                 media: dict[str, bytes] | None = None, meta: dict[str, dict] | None = None):
        self.files_data = files or []
        self.exports = exports or {}
        self.media = media or {}
        self.meta = meta or {}
        self.list_calls: list[dict[str, Any]] = []
        self.created: list[dict] = []

    def files(self):
        return self

    def list(self, **kw):
        self.list_calls.append(kw)
        return Req(lambda: {"files": self.files_data, "nextPageToken": None})

    def get(self, fileId, **kw):
        return Req(lambda: self.meta[fileId])

    def export(self, fileId, mimeType):
        return Req(lambda: self.exports[fileId])

    def get_media(self, fileId, **kw):
        return Req(lambda: self.media[fileId])

    def create(self, body, media_body, **kw):
        self.created.append({"body": body, "media": media_body})
        return Req(lambda: {"id": "new1", "name": body["name"], "mimeType": body.get("mimeType", media_body.mimetype()),
                            "webViewLink": "https://drive/new1"})


class FakeSheets:
    def __init__(self, sheets: list[tuple[str, list[list[str]], int]]):
        self.sheets = sheets  # (title, rows, gridRowCount)
        self.batch_ranges: list[str] = []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields):
        return Req(lambda: {"sheets": [{"properties": {"title": t, "sheetId": i, "gridProperties": {"rowCount": n}}}
                                       for i, (t, _, n) in enumerate(self.sheets)]})

    def values(self):
        return self

    def batchGet(self, spreadsheetId, ranges, valueRenderOption):  # noqa: N802
        self.batch_ranges = ranges

        def run():
            out = []
            for (title, rows, _), r in zip(self.sheets, ranges):
                cap = int(r.rsplit(":", 1)[1])
                out.append({"values": rows[:cap]})
            return {"valueRanges": out}

        return Req(run)


class FakeRunner:
    """ServiceRunner that hands out fake services or raises per account."""

    def __init__(self):
        self.services: dict[tuple[str, str], Any] = {}
        self.failures: dict[str, Exception] = {}
        self.calls: list[tuple[str, str]] = []

    async def run(self, account: Account, api: str, fn):
        self.calls.append((account.alias, api))
        if account.alias in self.failures:
            raise self.failures[account.alias]
        return fn(self.services[(account.alias, api)])
