"""Token refresh behaviour of the real GoogleBackend (Google calls mocked)."""

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from multi_google_mcp.google_auth import AccountAuthError, GoogleApiError, GoogleBackend


class FakeCreds:
    def __init__(self, refresh_exc=None):
        self.valid = False
        self.refresh_calls = 0
        self.refresh_exc = refresh_exc

    def refresh(self, request):
        self.refresh_calls += 1
        if self.refresh_exc:
            raise self.refresh_exc
        self.valid = True


class Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "x"


def make_backend(settings, store, creds_by_alias, builder=None):
    seen_tokens = {}

    def factory(account, refresh_token, s):
        seen_tokens[account.alias] = refresh_token
        return creds_by_alias[account.alias]

    backend = GoogleBackend(
        settings, store,
        credentials_factory=factory,
        service_builder=builder or (lambda api, creds: f"service:{api}"),
        request_factory=lambda: object(),
    )
    return backend, seen_tokens


async def test_refresh_once_then_cached(settings, store):
    creds = FakeCreds()
    backend, seen = make_backend(settings, store, {"work": creds})
    work = store.get("work")
    assert await backend.run(work, "gmail", lambda s: s) == "service:gmail"
    assert await backend.run(work, "drive", lambda s: s) == "service:drive"
    assert creds.refresh_calls == 1
    assert seen == {"work": "rt-work"}  # decrypted refresh token was used


async def test_revoked_refresh_token_gives_clear_error_and_marks_account(settings, store):
    creds = FakeCreds(RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}))
    backend, _ = make_backend(settings, store, {"work": creds})

    with pytest.raises(AccountAuthError) as ei:
        await backend.run(store.get("work"), "gmail", lambda s: s)

    msg = str(ei.value)
    assert "'work' (me@corp.com)" in msg
    assert "https://mcp.example.com/accounts/connect?alias=work" in msg
    assert ei.value.to_dict()["reconnect_url"].endswith("alias=work")
    assert store.get("work").status == "revoked"

    # Subsequent calls fail fast without hitting Google again.
    with pytest.raises(AccountAuthError):
        await backend.run(store.get("work"), "gmail", lambda s: s)
    assert creds.refresh_calls == 1


async def test_transient_refresh_error_is_not_treated_as_revoked(settings, store):
    creds = FakeCreds(RefreshError("temporarily_unavailable"))
    backend, _ = make_backend(settings, store, {"work": creds})
    with pytest.raises(GoogleApiError, match="Could not refresh"):
        await backend.run(store.get("work"), "gmail", lambda s: s)
    assert store.get("work").status == "connected"


async def test_http_error_is_compact(settings, store):
    backend, _ = make_backend(settings, store, {"work": FakeCreds()})

    def boom(_):
        raise HttpError(Resp(403), b'{"error": {"message": "Request had insufficient authentication scopes."}}')

    with pytest.raises(GoogleApiError) as ei:
        await backend.run(store.get("work"), "gmail", boom)
    assert str(ei.value).startswith("Google API error 403")
    assert "missing OAuth scope" in str(ei.value)


async def test_refresh_error_raised_mid_call_marks_revoked(settings, store):
    backend, _ = make_backend(settings, store, {"work": FakeCreds()})

    def fn(_):
        raise RefreshError("invalid_grant")

    with pytest.raises(AccountAuthError):
        await backend.run(store.get("work"), "gmail", fn)
    assert store.get("work").status == "revoked"


async def test_reconnect_clears_cached_credentials(settings, store):
    creds1, creds2 = FakeCreds(), FakeCreds()
    seq = iter([creds1, creds2])
    backend = GoogleBackend(settings, store, credentials_factory=lambda a, rt, s: next(seq),
                            service_builder=lambda api, c: c, request_factory=object)
    work = store.get("work")
    assert await backend.run(work, "gmail", lambda c: c) is creds1
    backend.invalidate("work")
    assert await backend.run(work, "gmail", lambda c: c) is creds2
