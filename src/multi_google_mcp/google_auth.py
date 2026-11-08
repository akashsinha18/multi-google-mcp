"""Per-account Google credentials, automatic refresh and API service factory.

All Google client library calls are blocking, so `GoogleBackend.run` executes
them in a worker thread. A fresh service object is built per call because
googleapiclient's httplib2 transport is not thread-safe.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Protocol, TypeVar

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Settings
from .store import STATUS_REVOKED, Account, AccountStore

log = logging.getLogger(__name__)

T = TypeVar("T")

TOKEN_URI = "https://oauth2.googleapis.com/token"

API_VERSIONS: dict[str, str] = {
    "gmail": "v1",
    "drive": "v3",
    "sheets": "v4",
}


class AccountError(Exception):
    """Base class for errors tied to one account (safe to show to the user)."""

    def __init__(self, account: Account, message: str):
        super().__init__(message)
        self.account = account
        self.message = message

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {"account": self.account.ref, "error": self.message}


class AccountAuthError(AccountError):
    """The account's refresh token is revoked/expired; it must be reconnected."""

    def __init__(self, account: Account, reconnect_url: str, detail: str = ""):
        msg = (
            f"Google account '{account.alias}' ({account.email}) needs to be "
            f"reconnected: its authorization was revoked or has expired"
            + (f" ({detail})" if detail else "")
            + f". Reconnect at {reconnect_url}"
        )
        super().__init__(account, msg)
        self.reconnect_url = reconnect_url

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["reconnect_url"] = self.reconnect_url
        return d


class GoogleApiError(AccountError):
    """A Google API call failed for one account (compact, no payloads)."""


def describe_http_error(e: HttpError) -> str:
    status = getattr(e.resp, "status", None)
    reason = ""
    try:
        reason = e._get_reason() or ""  # noqa: SLF001 - compact message only
    except Exception:  # pragma: no cover - defensive
        reason = ""
    reason = reason.strip().splitlines()[0][:300] if reason else ""
    hint = ""
    if status == 403 and "insufficient" in reason.lower():
        hint = " (missing OAuth scope: reconnect the account or check the SCOPES list)"
    elif status == 404:
        hint = " (not found in this account)"
    elif status == 429:
        hint = " (rate limited; retry shortly)"
    return f"Google API error {status}: {reason}{hint}".rstrip()


def _is_revoked(e: RefreshError) -> bool:
    text = str(e).lower()
    return "invalid_grant" in text or "token has been expired or revoked" in text


class ServiceRunner(Protocol):
    """Interface used by gmail/drive modules (mocked in tests)."""

    async def run(self, account: Account, api: str, fn: Callable[[Any], T]) -> T: ...


CredentialsFactory = Callable[[Account, str, Settings], Credentials]
ServiceBuilder = Callable[[str, Credentials], Any]


def default_credentials_factory(account: Account, refresh_token: str, settings: Settings) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=list(account.scopes) or None,
    )


def default_service_builder(api: str, creds: Credentials) -> Any:
    return build(api, API_VERSIONS[api], credentials=creds, cache_discovery=False)


class GoogleBackend:
    def __init__(
        self,
        settings: Settings,
        store: AccountStore,
        credentials_factory: CredentialsFactory = default_credentials_factory,
        service_builder: ServiceBuilder = default_service_builder,
        request_factory: Callable[[], Any] = Request,
    ):
        self._settings = settings
        self._store = store
        self._credentials_factory = credentials_factory
        self._service_builder = service_builder
        self._request_factory = request_factory
        self._creds: dict[str, Credentials] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def invalidate(self, alias: str) -> None:
        with self._guard:
            self._creds.pop(alias, None)

    def _lock_for(self, alias: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(alias, threading.Lock())

    def _auth_error(self, account: Account, detail: str = "") -> AccountAuthError:
        return AccountAuthError(account, self._settings.connect_url(account.alias), detail)

    def credentials(self, account: Account) -> Credentials:
        """Return valid credentials, refreshing if needed (blocking)."""
        with self._lock_for(account.alias):
            creds = self._creds.get(account.alias)
            if creds is None:
                refresh_token = self._store.get_refresh_token(account.alias)
                if not refresh_token:
                    raise self._auth_error(account, "no stored refresh token")
                creds = self._credentials_factory(account, refresh_token, self._settings)
                self._creds[account.alias] = creds
            if not creds.valid:
                log.info("Refreshing access token for account %s", account.alias)
                creds.refresh(self._request_factory())
            return creds

    def _handle_refresh_error(self, account: Account, e: RefreshError) -> AccountError:
        self.invalidate(account.alias)
        if _is_revoked(e):
            log.warning("Refresh token revoked/expired for account %s", account.alias)
            self._store.mark_status(account.alias, STATUS_REVOKED, "refresh token revoked or expired")
            return self._auth_error(account)
        log.warning("Token refresh failed for account %s: %s", account.alias, type(e).__name__)
        return GoogleApiError(account, f"Could not refresh Google access token for '{account.alias}': {str(e)[:200]}")

    async def run(self, account: Account, api: str, fn: Callable[[Any], T]) -> T:
        if account.status == STATUS_REVOKED:
            raise self._auth_error(account)

        def work() -> T:
            creds = self.credentials(account)
            service = self._service_builder(api, creds)
            return fn(service)

        try:
            return await asyncio.to_thread(work)
        except AccountError:
            raise
        except RefreshError as e:
            raise self._handle_refresh_error(account, e) from None
        except HttpError as e:
            if getattr(e.resp, "status", None) == 401:
                # Access token rejected mid-call: drop cache so next call refreshes.
                self.invalidate(account.alias)
            raise GoogleApiError(account, describe_http_error(e)) from None
        except TransportError as e:
            raise GoogleApiError(account, f"Network error talking to Google: {type(e).__name__}") from None
