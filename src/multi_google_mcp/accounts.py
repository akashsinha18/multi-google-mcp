"""Resolving the `account` tool argument to stored accounts."""

from __future__ import annotations

from .store import Account, AccountStore

ALL = "all"


class AccountNotFoundError(ValueError):
    pass


def resolve_accounts(
    store: AccountStore,
    account: str | None,
    *,
    allow_all: bool,
    connect_hint_url: str | None = None,
) -> list[Account]:
    """Resolve an alias, an email address, or "all" to account records.

    Matching is case-insensitive and ignores surrounding whitespace.
    """
    accounts = store.list()
    if not accounts:
        hint = f" Connect one at {connect_hint_url}" if connect_hint_url else ""
        raise AccountNotFoundError(f"No Google accounts are connected yet.{hint}")

    key = (account or "").strip().lower()
    if not key:
        raise AccountNotFoundError(_unknown_msg(account, accounts, allow_all))

    if key == ALL:
        if not allow_all:
            raise AccountNotFoundError(
                "This tool needs a single account; 'all' is not supported here. "
                + _available(accounts)
            )
        return accounts

    for a in accounts:
        if a.alias == key:
            return [a]
    for a in accounts:
        if a.email.lower() == key:
            return [a]
    raise AccountNotFoundError(_unknown_msg(account, accounts, allow_all))


def resolve_one(store: AccountStore, account: str, connect_hint_url: str | None = None) -> Account:
    return resolve_accounts(store, account, allow_all=False, connect_hint_url=connect_hint_url)[0]


def _available(accounts: list[Account]) -> str:
    return "Available: " + ", ".join(f"{a.alias} ({a.email})" for a in accounts)


def _unknown_msg(account: str | None, accounts: list[Account], allow_all: bool) -> str:
    extra = " or 'all'" if allow_all else ""
    return f"Unknown account {account!r}. Use an alias or email{extra}. " + _available(accounts)
