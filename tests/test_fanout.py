import pytest

from multi_google_mcp import gmail
from multi_google_mcp.fanout import decode_page_tokens, encode_page_tokens, fan_out, merge_sorted
from multi_google_mcp.google_auth import AccountAuthError, GoogleApiError
from multi_google_mcp.store import Account

from .fakes import FakeGmail, gmail_message

P = Account("personal", "me@gmail.com", "connected")
W = Account("work", "me@corp.com", "connected")


def test_merge_sorted_tags_and_orders_by_date():
    merged = merge_sorted(
        [
            (P, [{"id": "p1", "date": "2026-01-03"}, {"id": "p2", "date": "2026-01-01"}]),
            (W, [{"id": "w1", "date": "2026-01-02"}, {"id": "w2", "date": None}]),
        ],
        sort_key="date",
    )
    assert [m["id"] for m in merged] == ["p1", "w1", "p2", "w2"]
    assert merged[1]["account"] == {"alias": "work", "email": "me@corp.com"}


async def test_fan_out_partial_failure():
    async def fn(a):
        if a.alias == "work":
            raise GoogleApiError(a, "Google API error 500: boom")
        return [1]

    ok, errors = await fan_out([P, W], fn)
    assert ok == [(P, [1])]
    assert errors == [{"account": W.ref, "error": "Google API error 500: boom"}]


async def test_fan_out_unexpected_exception_becomes_error_entry():
    async def fn(a):
        raise RuntimeError("kaput")

    ok, errors = await fan_out([P], fn)
    assert ok == [] and errors[0]["error"] == "RuntimeError: kaput"


def test_page_token_roundtrip_drops_exhausted():
    tok = encode_page_tokens({"personal": "abc", "work": None})
    assert decode_page_tokens(tok) == {"personal": "abc"}
    assert encode_page_tokens({"a": None}) is None


def test_page_token_rejects_garbage():
    with pytest.raises(ValueError):
        decode_page_tokens("raw-google-token")
    with pytest.raises(ValueError):
        decode_page_tokens("mt1.!!!notbase64")


async def test_gmail_search_all_merges_and_tags(ctx, runner):
    runner.services[("personal", "gmail")] = FakeGmail(
        [gmail_message("p1", 3000, "P newest"), gmail_message("p2", 1000, "P oldest")], page_size_token="ptok"
    )
    runner.services[("work", "gmail")] = FakeGmail([gmail_message("w1", 2000, "W middle")])

    res = await gmail.search(ctx, "is:unread", account="all", max_results=10)

    assert [m["subject"] for m in res["messages"]] == ["P newest", "W middle", "P oldest"]
    assert [m["account"]["alias"] for m in res["messages"]] == ["personal", "work", "personal"]
    assert res["messages"][0]["date"].endswith("Z")
    assert "errors" not in res
    # Only personal has more pages
    assert decode_page_tokens(res["next_page_token"]) == {"personal": "ptok"}


async def test_gmail_search_all_continuation_only_queries_accounts_with_tokens(ctx, runner):
    pg = FakeGmail([gmail_message("p3", 500, "page2")])
    runner.services[("personal", "gmail")] = pg
    runner.services[("work", "gmail")] = FakeGmail([])
    token = encode_page_tokens({"personal": "ptok"})

    res = await gmail.search(ctx, "x", account="all", page_token=token)

    assert ("work", "gmail") not in runner.calls
    assert pg.list_calls[0]["pageToken"] == "ptok"
    assert [m["id"] for m in res["messages"]] == ["p3"]
    assert res["next_page_token"] is None


async def test_gmail_search_all_partial_failure_with_reconnect_url(ctx, runner, settings):
    runner.services[("personal", "gmail")] = FakeGmail([gmail_message("p1", 1, "ok")])
    work = ctx.store.get("work")
    runner.failures["work"] = AccountAuthError(work, settings.connect_url("work"))

    res = await gmail.search(ctx, "x", account="all")

    assert [m["id"] for m in res["messages"]] == ["p1"]
    [err] = res["errors"]
    assert err["account"] == {"alias": "work", "email": "me@corp.com"}
    assert err["reconnect_url"] == "https://mcp.example.com/accounts/connect?alias=work"
    assert "'work' (me@corp.com) needs to be reconnected" in err["error"]


async def test_gmail_search_single_account_uses_raw_token(ctx, runner):
    g = FakeGmail([gmail_message("w1", 1, "s")], page_size_token="next")
    runner.services[("work", "gmail")] = g
    res = await gmail.search(ctx, "x", account="me@corp.com", page_token="rawtok")
    assert g.list_calls[0]["pageToken"] == "rawtok"
    assert res["account"]["alias"] == "work"
    assert res["next_page_token"] == "next"
    assert res["messages"][0]["account"]["alias"] == "work"


async def test_gmail_search_single_account_failure_raises(ctx, runner):
    work = ctx.store.get("work")
    runner.failures["work"] = GoogleApiError(work, "Google API error 500: boom")
    with pytest.raises(gmail.GmailError, match="boom"):
        await gmail.search(ctx, "x", account="work")
