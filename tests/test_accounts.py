import sqlite3

import pytest

from multi_google_mcp.accounts import AccountNotFoundError, resolve_accounts
from multi_google_mcp.store import AccountStore, StoreError, normalize_alias


def test_resolve_by_alias_case_insensitive(store):
    [a] = resolve_accounts(store, "  WORK ", allow_all=True)
    assert a.alias == "work" and a.email == "me@corp.com"


def test_resolve_by_email(store):
    [a] = resolve_accounts(store, "me@gmail.com", allow_all=False)
    assert a.alias == "personal"
    assert a.ref == {"alias": "personal", "email": "me@gmail.com"}


def test_resolve_all(store):
    accounts = resolve_accounts(store, "all", allow_all=True)
    assert [a.alias for a in accounts] == ["personal", "work"]


def test_all_rejected_for_single_account_tools(store):
    with pytest.raises(AccountNotFoundError, match="'all' is not supported"):
        resolve_accounts(store, "all", allow_all=False)


def test_unknown_account_lists_available(store):
    with pytest.raises(AccountNotFoundError) as ei:
        resolve_accounts(store, "school", allow_all=True)
    msg = str(ei.value)
    assert "personal (me@gmail.com)" in msg and "work (me@corp.com)" in msg


def test_no_accounts_connected(settings, crypto):
    empty = AccountStore(":memory:", crypto)
    with pytest.raises(AccountNotFoundError, match="No Google accounts are connected.*https://x/accounts"):
        resolve_accounts(empty, "all", allow_all=True, connect_hint_url="https://x/accounts")


@pytest.mark.parametrize("bad", ["", "all", "ALL", "has space", "-lead", "x" * 33, "émoji"])
def test_invalid_aliases(bad):
    with pytest.raises(StoreError):
        normalize_alias(bad)


def test_refresh_token_encrypted_at_rest(store, settings):
    raw = sqlite3.connect(settings.db_path).execute(
        "SELECT refresh_token FROM accounts WHERE alias='work'"
    ).fetchone()[0]
    assert "rt-work" not in raw
    assert store.get_refresh_token("work") == "rt-work"


def test_same_email_under_second_alias_rejected(store):
    with pytest.raises(StoreError, match="already connected as alias 'work'"):
        store.upsert("job", "ME@corp.com", "rt", [])


def test_reconnect_same_alias_updates_and_resets_status(store):
    store.mark_status("work", "revoked", "x")
    store.upsert("work", "me@corp.com", "rt-new", ["s"])
    a = store.get("work")
    assert a.status == "connected" and a.last_error is None
    assert store.get_refresh_token("work") == "rt-new"
