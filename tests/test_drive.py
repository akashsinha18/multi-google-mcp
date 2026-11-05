import pytest

from multi_google_mcp import drive
from multi_google_mcp.drive import build_search_query, escape_drive_string
from multi_google_mcp.fanout import decode_page_tokens
from multi_google_mcp.google_auth import GoogleApiError

from .fakes import FakeDrive, FakeSheets


# ---------------------------------------------------------------- escaping
def test_escape_quotes_and_backslashes():
    assert escape_drive_string("O'Brien") == "O\\'Brien"
    assert escape_drive_string("a\\b") == "a\\\\b"
    # backslash escaped before quote, so \' can't become an unescaped quote
    assert escape_drive_string("\\'") == "\\\\\\'"


def test_build_query_keywords_and_phrases():
    q = build_search_query('budget "q3 plan"')
    assert q == (
        "(name contains 'budget' or fullText contains 'budget') and "
        "(name contains 'q3 plan' or fullText contains 'q3 plan') and trashed = false"
    )


def test_build_query_injection_is_neutralised():
    q = build_search_query("x' or trashed = true or name contains '")
    # Every single quote from the input is escaped; the only unescaped quotes are ours.
    unescaped = [i for i, c in enumerate(q) if c == "'" and q[i - 1] != "\\"]
    assert len(unescaped) % 2 == 0
    assert "x\\'" in q and q.endswith("trashed = false")


def test_build_query_empty():
    assert build_search_query("   ") == "trashed = false"


# ---------------------------------------------------------------- search
async def test_drive_search_includes_shared_drives_and_fans_out(ctx, runner):
    p = FakeDrive(files=[{"id": "f1", "name": "A", "mimeType": "text/plain", "modifiedTime": "2026-01-01T00:00:00Z"}])
    w = FakeDrive(files=[{"id": "f2", "name": "B", "mimeType": "text/plain", "modifiedTime": "2026-02-01T00:00:00Z",
                          "driveId": "sd1", "size": "12"}])
    runner.services[("personal", "drive")] = p
    runner.services[("work", "drive")] = w

    res = await drive.search(ctx, "it's", account="all")

    call = p.list_calls[0]
    assert call["supportsAllDrives"] is True and call["includeItemsFromAllDrives"] is True
    assert call["corpora"] == "allDrives"
    assert "it\\'s" in call["q"]
    assert [f["id"] for f in res["files"]] == ["f2", "f1"]
    assert res["files"][0]["account"]["alias"] == "work"
    assert res["files"][0]["shared_drive_id"] == "sd1" and res["files"][0]["size"] == 12
    assert res["next_page_token"] is None


async def test_drive_list_recent_partial_failure(ctx, runner):
    runner.services[("personal", "drive")] = FakeDrive(files=[{"id": "f1", "name": "A", "modifiedTime": "2026"}])
    runner.failures["work"] = GoogleApiError(ctx.store.get("work"), "Google API error 500: x")
    res = await drive.list_recent(ctx, account="all")
    assert [f["id"] for f in res["files"]] == ["f1"]
    assert res["errors"][0]["account"]["alias"] == "work"


async def test_drive_single_account_page_token(ctx, runner):
    d = FakeDrive(files=[])
    runner.services[("work", "drive")] = d
    res = await drive.list_recent(ctx, account="work", page_token="g-tok")
    assert d.list_calls[0]["pageToken"] == "g-tok"
    assert d.list_calls[0]["orderBy"] == "modifiedTime desc"
    assert res["account"]["alias"] == "work"


# ---------------------------------------------------------------- read + truncation
async def test_read_doc_as_markdown_truncated(ctx, runner):
    text = ("word " * 2000).encode()
    runner.services[("work", "drive")] = FakeDrive(
        meta={"d1": {"id": "d1", "name": "Doc", "mimeType": drive.GDOC}}, exports={"d1": text}
    )
    res = await drive.read_file(ctx, "d1", "work", max_chars=500)
    assert res["format"] == "markdown"
    assert res["truncated"] is True
    assert res["total_chars"] > 500
    assert len(res["content"]) <= 500 + len("\n…[truncated]")
    assert res["account"] == {"alias": "work", "email": "me@corp.com"}


async def test_read_small_text_not_truncated(ctx, runner):
    runner.services[("work", "drive")] = FakeDrive(
        meta={"t1": {"id": "t1", "name": "n.txt", "mimeType": "text/plain", "size": "5"}}, media={"t1": b"hello"}
    )
    res = await drive.read_file(ctx, "t1", "work")
    assert res["content"] == "hello" and res["truncated"] is False


async def test_read_sheet_csv_per_sheet_with_row_cap(ctx, runner):
    runner.services[("work", "drive")] = FakeDrive(meta={"s1": {"id": "s1", "name": "S", "mimeType": drive.GSHEET}})
    sheets = FakeSheets([
        ("Data", [["a", "b"], ["1", "2,3"], ["4", "5"]], 1000),
        ("It's", [["x"]], 1),
    ])
    runner.services[("work", "sheets")] = sheets
    res = await drive.read_file(ctx, "s1", "work", sheet_row_cap=2)
    assert res["format"] == "csv"
    data, other = res["sheets"]
    assert data["csv"] == 'a,b\n1,"2,3"\n'
    assert data["rows_truncated"] is True and res["truncated"] is True
    assert other["sheet"] == "It's" and other["rows_truncated"] is False
    assert sheets.batch_ranges[1] == "'It''s'!1:2"


async def test_read_unsupported_returns_metadata(ctx, runner):
    runner.services[("work", "drive")] = FakeDrive(
        meta={"z": {"id": "z", "name": "a.zip", "mimeType": "application/zip", "size": "10"}}
    )
    res = await drive.read_file(ctx, "z", "work")
    assert res["supported"] is False and res["file"]["name"] == "a.zip"
    assert "content" not in res


async def test_create_file_converts_to_google_doc(ctx, runner):
    d = FakeDrive()
    runner.services[("work", "drive")] = d
    res = await drive.create_file(ctx, "work", "Notes", "hi", mime_type=drive.GDOC, folder_id="fold")
    body = d.created[0]["body"]
    assert body == {"name": "Notes", "mimeType": drive.GDOC, "parents": ["fold"]}
    assert d.created[0]["media"].mimetype() == "text/plain"
    assert res["file"]["id"] == "new1" and res["account"]["alias"] == "work"


async def test_create_file_rejects_binary_types(ctx):
    with pytest.raises(drive.DriveError):
        await drive.create_file(ctx, "work", "x", "y", mime_type="image/png")
