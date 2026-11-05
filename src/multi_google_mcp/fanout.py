"""Parallel per-account execution with partial-failure handling."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable, TypeVar

from .google_auth import AccountError
from .store import Account

log = logging.getLogger(__name__)

T = TypeVar("T")


def error_entry(account: Account, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AccountError):
        return exc.to_dict()
    # Unexpected error: report type + short message only (never payloads).
    log.exception("Unexpected error for account %s", account.alias, exc_info=exc)
    return {"account": account.ref, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def fan_out(
    accounts: list[Account],
    fn: Callable[[Account], Awaitable[T]],
) -> tuple[list[tuple[Account, T]], list[dict[str, Any]]]:
    """Run `fn` for every account concurrently.

    Returns (successes, errors). A failure in one account never fails the
    whole call; it becomes an entry in `errors`.
    """
    results = await asyncio.gather(*(fn(a) for a in accounts), return_exceptions=True)
    ok: list[tuple[Account, T]] = []
    errors: list[dict[str, Any]] = []
    for account, res in zip(accounts, results):
        if isinstance(res, BaseException):
            if not isinstance(res, Exception):  # CancelledError, KeyboardInterrupt...
                raise res
            errors.append(error_entry(account, res))
        else:
            ok.append((account, res))
    return ok, errors


def merge_sorted(
    per_account: list[tuple[Account, list[dict[str, Any]]]],
    sort_key: str,
    descending: bool = True,
) -> list[dict[str, Any]]:
    """Tag each item with its account and merge by `sort_key` (missing keys last)."""
    merged: list[dict[str, Any]] = []
    for account, items in per_account:
        for item in items:
            merged.append({"account": account.ref, **item})
    present = [m for m in merged if m.get(sort_key) is not None]
    missing = [m for m in merged if m.get(sort_key) is None]
    present.sort(key=lambda m: m[sort_key], reverse=descending)
    return present + missing


# ---------------------------------------------------------------------------
# Composite page tokens for fan-out pagination: {alias: provider_page_token}.
# ---------------------------------------------------------------------------
_PREFIX = "mt1."


def encode_page_tokens(tokens: dict[str, str]) -> str | None:
    tokens = {k: v for k, v in tokens.items() if v}
    if not tokens:
        return None
    raw = json.dumps(tokens, separators=(",", ":"), sort_keys=True).encode()
    return _PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_page_tokens(token: str) -> dict[str, str]:
    if not token.startswith(_PREFIX):
        raise ValueError(
            "page_token is not a multi-account token; pass the next_page_token "
            "returned by the previous call with the same account argument."
        )
    body = token[len(_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError("page_token is malformed") from e
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError("page_token is malformed")
    return data
