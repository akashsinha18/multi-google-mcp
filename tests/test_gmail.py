import base64
import email

import pytest

from multi_google_mcp import gmail
from multi_google_mcp.text import html_to_text, truncate

from .fakes import FakeGmail, b64, gmail_message


def test_truncate_flag_and_limit():
    assert truncate("short", 100) == ("short", False)
    text, cut = truncate("abc " * 100, 50)
    assert cut is True and text.endswith("…[truncated]") and len(text) <= 50 + 13


def test_html_to_text():
    out = html_to_text("<p>Hello <b>world</b></p><script>x</script><p>Line&nbsp;2</p>")
    assert "Hello world" in out and "Line" in out and "<" not in out


def _multipart(plain=None, html=None, attachment=None):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
                      "body": {"data": b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    if attachment:
        parts.append({"mimeType": "application/pdf", "filename": attachment,
                      "body": {"attachmentId": "att1", "size": 1234}})
    return {"mimeType": "multipart/mixed", "headers": [
        {"name": "From", "value": "Alice <a@x.com>"}, {"name": "To", "value": "Me <me@x.com>, b@x.com"},
        {"name": "Subject", "value": "Hi"}], "parts": parts}


def test_parse_message_prefers_plain_and_lists_attachments():
    msg = gmail_message("m1", 1, "Hi", payload=_multipart(plain="plain body", html="<p>html</p>", attachment="r.pdf"))
    out = gmail.parse_message(msg, 1000)
    assert out["body"] == "plain body" and out["body_truncated"] is False
    assert out["attachments"] == [{"filename": "r.pdf", "mime_type": "application/pdf", "size": 1234}]
    assert out["to"] == ["Me <me@x.com>", "b@x.com"]


def test_parse_message_html_only_converted():
    msg = gmail_message("m1", 1, "Hi", payload=_multipart(html="<div>Only <i>html</i></div>"))
    assert gmail.parse_message(msg, 1000)["body"] == "Only html"


def test_parse_message_truncates_long_body():
    msg = gmail_message("m1", 1, "Hi", payload=_multipart(plain="x " * 5000))
    out = gmail.parse_message(msg, 300)
    assert out["body_truncated"] is True and out["body_total_chars"] > 300


async def test_get_thread(ctx, runner):
    g = FakeGmail([])
    g.threads_data["t1"] = {"id": "t1", "messages": [gmail_message("m1", 1, "Subj", labels=["INBOX", "UNREAD"])]}
    runner.services[("personal", "gmail")] = g
    res = await gmail.get_thread(ctx, "t1", "personal")
    assert res["account"]["alias"] == "personal"
    assert res["subject"] == "Subj" and res["messages"][0]["unread"] is True


async def test_modify_labels_resolves_names_and_falls_back_to_message(ctx, runner):
    g = FakeGmail([])
    runner.services[("work", "gmail")] = g
    res = await gmail.modify_labels(ctx, "m9", "work", add=["receipts"], remove=["unread", "INBOX"])
    kind, mid, body = g.modify_calls[0]
    assert kind == "message" and body == {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD", "INBOX"]}
    assert res["target"] == "message" and res["added"] == ["Receipts"]


async def test_modify_labels_thread(ctx, runner):
    g = FakeGmail([])
    g.threads_data["t1"] = {"id": "t1"}
    runner.services[("work", "gmail")] = g
    res = await gmail.modify_labels(ctx, "t1", "work", remove=["INBOX"])
    assert res["target"] == "thread" and g.modify_calls[0][0] == "thread"


async def test_modify_labels_unknown_label(ctx, runner):
    runner.services[("work", "gmail")] = FakeGmail([])
    with pytest.raises(gmail.GmailError, match="Unknown label"):
        await gmail.modify_labels(ctx, "t1", "work", add=["Nope"])


async def test_create_reply_draft_sets_threading_headers(ctx, runner):
    g = FakeGmail([])
    g.threads_data["t1"] = {"id": "t1", "messages": [{"payload": {"headers": [
        {"name": "Message-ID", "value": "<orig@x>"}, {"name": "Subject", "value": "Plans"}]}}]}
    runner.services[("work", "gmail")] = g
    res = await gmail.create_draft(ctx, "work", ["bob@x.com"], "", "Sounds good", cc=["c@x.com"],
                                   reply_to_thread_id="t1")
    msg = g.drafts_created[0]["message"]
    assert msg["threadId"] == "t1"
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(msg["raw"]))
    assert parsed["Subject"] == "Re: Plans"
    assert parsed["In-Reply-To"] == "<orig@x>" and parsed["From"] == "me@corp.com"
    assert parsed["Cc"] == "c@x.com"
    assert res["draft_id"] == "d1" and res["status"] == "draft_created"


async def test_send_draft(ctx, runner):
    runner.services[("work", "gmail")] = FakeGmail([])
    res = await gmail.send_draft(ctx, "d1", "work")
    assert res["status"] == "sent" and res["account"]["email"] == "me@corp.com"


async def test_single_account_tools_reject_all(ctx):
    from multi_google_mcp.accounts import AccountNotFoundError
    with pytest.raises(AccountNotFoundError):
        await gmail.send_draft(ctx, "d1", "all")


async def test_no_bodies_or_tokens_in_logs(ctx, runner, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    g = FakeGmail([])
    g.threads_data["t1"] = {"id": "t1", "messages": [gmail_message("m1", 1, "TopSecretSubject")]}
    runner.services[("work", "gmail")] = g
    await gmail.get_thread(ctx, "t1", "work")
    assert "body of TopSecretSubject" not in caplog.text
    assert "rt-work" not in caplog.text
