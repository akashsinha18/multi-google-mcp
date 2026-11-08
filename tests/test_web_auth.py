"""MCP allowlist and /accounts route protection."""

from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from multi_google_mcp.auth import is_email_allowed
from multi_google_mcp.server import create_app
from multi_google_mcp.web import SESSION_COOKIE

ALLOWED = frozenset({"me@example.com"})


@pytest.mark.parametrize(
    "claims,ok",
    [
        ({"email": "Me@Example.com", "email_verified": True}, True),
        ({"email": "me@example.com", "email_verified": "true"}, True),
        ({"email": "me@example.com", "email_verified": False}, False),
        ({"email": "other@example.com", "email_verified": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_email_allowed(claims, ok):
    assert is_email_allowed(claims, ALLOWED) is ok


@pytest.fixture
def client(settings, store):
    app, _, ctx = create_app(settings)
    with TestClient(app, base_url="https://mcp.example.com") as c:
        c.ctx = ctx
        yield c


def test_mcp_requires_auth(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_protected_resource_metadata_advertised(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json()["resource"].startswith("https://mcp.example.com")


def test_connect_without_session_redirects_to_login(client):
    r = client.get("/accounts/connect?alias=work", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/accounts/login?next=")


def test_accounts_index_without_session_redirects(client):
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 302


def test_forged_session_cookie_rejected(client):
    client.cookies.set(SESSION_COOKIE, "not-a-real-token", path="/accounts")
    r = client.get("/accounts/connect?alias=work", follow_redirects=False)
    assert r.headers["location"].startswith("/accounts/login")


def test_session_for_non_allowed_email_rejected(client, crypto):
    client.cookies.set(SESSION_COOKIE, crypto.seal({"purpose": "session", "email": "evil@x.com"}), path="/accounts")
    r = client.get("/accounts/connect?alias=work", follow_redirects=False)
    assert r.headers["location"].startswith("/accounts/login")


def test_connect_with_session_starts_google_oauth(client, crypto, settings):
    client.cookies.set(SESSION_COOKIE, crypto.seal({"purpose": "session", "email": "me@example.com"}), path="/accounts")
    r = client.get("/accounts/connect?alias=Work", follow_redirects=False)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "accounts.google.com"
    assert q["access_type"] == ["offline"]
    assert "consent" in q["prompt"][0]
    assert q["redirect_uri"] == ["https://mcp.example.com/accounts/callback"]
    assert q["code_challenge_method"] == ["S256"]
    for s in settings.scopes:
        assert s in q["scope"][0]
    assert q["login_hint"] == ["me@corp.com"]  # existing alias -> hint its email
    state = crypto.unseal(q["state"][0], 600)
    assert state["alias"] == "work" and state["purpose"] == "connect"


def test_callback_rejects_state_without_matching_nonce_cookie(client, crypto):
    state = crypto.seal({"purpose": "connect", "alias": "work", "nonce": "abc", "cv": "v"})
    r = client.get(f"/accounts/callback?state={state}&code=c")
    assert r.status_code == 400 and "Invalid or expired" in r.text


def test_invalid_alias_rejected(client, crypto):
    client.cookies.set(SESSION_COOKIE, crypto.seal({"purpose": "session", "email": "me@example.com"}), path="/accounts")
    r = client.get("/accounts/connect?alias=all", follow_redirects=False)
    assert r.status_code == 400


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
