"""Web routes for connecting Google data accounts.

Access control: every /accounts route requires an admin session cookie. The
session is created by signing in with Google (identity scopes only) as one of
ALLOWED_EMAILS. The data account being connected can be *any* Google account
(e.g. a work account), because the admin session — not the connected account —
authorizes the operation.

OAuth state is Fernet-sealed (tamper-proof, 10 min TTL) and bound to the
browser with a nonce cookie (CSRF protection). The PKCE verifier travels
inside the sealed state.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
from typing import Any
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import is_email_allowed
from .config import IDENTITY_SCOPES, Settings
from .crypto import Crypto
from .google_auth import GoogleBackend
from .store import AccountStore, StoreError, normalize_alias

log = logging.getLogger(__name__)

# Google may return scopes in a different order/form than requested.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SESSION_COOKIE = "mgm_session"
NONCE_COOKIE = "mgm_oauth_nonce"
SESSION_TTL = 12 * 3600
STATE_TTL = 600


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;line-height:1.5}}
 table{{border-collapse:collapse;width:100%}} td,th{{padding:.4rem;border-bottom:1px solid #ddd;text-align:left}}
 .ok{{color:#137333}} .bad{{color:#b3261e}} input,button{{font-size:1rem;padding:.3rem .5rem}}
 code{{background:#f1f3f4;padding:0 .2rem}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


class AccountsWeb:
    def __init__(self, settings: Settings, store: AccountStore, backend: GoogleBackend, crypto: Crypto):
        self.settings = settings
        self.store = store
        self.backend = backend
        self.crypto = crypto
        self.secure_cookies = settings.base_url.startswith("https://")
        if settings.base_url.startswith(("http://localhost", "http://127.0.0.1")):
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    # -- helpers ----------------------------------------------------------
    def _client_config(self) -> dict[str, Any]:
        return {
            "web": {
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [self.settings.accounts_redirect_uri],
            }
        }

    def _flow(self, scopes: list[str], code_verifier: str | None = None) -> Flow:
        return Flow.from_client_config(
            self._client_config(),
            scopes=scopes,
            redirect_uri=self.settings.accounts_redirect_uri,
            code_verifier=code_verifier,
            autogenerate_code_verifier=code_verifier is None,
        )

    def _session_email(self, request: Request) -> str | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        data = self.crypto.unseal(raw, SESSION_TTL)
        if not data or data.get("purpose") != "session":
            return None
        email = (data.get("email") or "").lower()
        return email if email in self.settings.allowed_emails else None

    def _set_cookie(self, resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=self.secure_cookies,
                        samesite="lax", path="/accounts")

    def _start_oauth(self, scopes: list[str], state_payload: dict[str, Any],
                     extra: dict[str, str]) -> Response:
        flow = self._flow(scopes)
        nonce = secrets.token_urlsafe(24)
        state = self.crypto.seal({**state_payload, "nonce": nonce, "cv": flow.code_verifier})
        url, _ = flow.authorization_url(state=state, **extra)
        resp = RedirectResponse(url, status_code=302)
        self._set_cookie(resp, NONCE_COOKIE, nonce, STATE_TTL)
        return resp

    def _login_redirect(self, next_path: str) -> Response:
        return RedirectResponse(f"/accounts/login?next={quote(next_path)}", status_code=302)

    def _exchange(self, scopes: list[str], code_verifier: str, code: str) -> tuple[Any, dict[str, Any]]:
        """Blocking: exchange the code and verify the ID token."""
        flow = self._flow(scopes, code_verifier=code_verifier)
        flow.fetch_token(code=code)
        creds = flow.credentials
        if not creds.id_token:
            raise ValueError("Google did not return an ID token")
        claims = google_id_token.verify_oauth2_token(
            creds.id_token, GoogleRequest(), audience=self.settings.google_client_id
        )
        return creds, claims

    # -- routes -----------------------------------------------------------
    async def index(self, request: Request) -> Response:
        admin = self._session_email(request)
        if not admin:
            return self._login_redirect("/accounts")
        rows = []
        for a in self.store.list():
            cls = "ok" if a.status == "connected" else "bad"
            rows.append(
                f"<tr><td><b>{html.escape(a.alias)}</b></td><td>{html.escape(a.email)}</td>"
                f"<td class='{cls}'>{html.escape(a.status)}</td>"
                f"<td><a href='/accounts/connect?alias={quote(a.alias)}'>reconnect</a> · "
                f"<form method='post' action='/accounts/disconnect' style='display:inline'>"
                f"<input type='hidden' name='alias' value='{html.escape(a.alias)}'>"
                f"<button onclick=\"return confirm('Disconnect {html.escape(a.alias)}?')\">disconnect</button>"
                f"</form></td></tr>"
            )
        table = ("<table><tr><th>Alias</th><th>Email</th><th>Status</th><th></th></tr>"
                 + "".join(rows) + "</table>") if rows else "<p>No accounts connected yet.</p>"
        body = (
            f"<p>Signed in as <b>{html.escape(admin)}</b>.</p>{table}"
            "<h2>Connect an account</h2>"
            "<form method='get' action='/accounts/connect'>"
            "<input name='alias' placeholder='alias, e.g. work' pattern='[a-z0-9][a-z0-9_-]{0,31}' required> "
            "<button>Connect with Google</button></form>"
        )
        return _page("Connected Google accounts", body)

    async def login(self, request: Request) -> Response:
        next_path = request.query_params.get("next") or "/accounts"
        if not next_path.startswith("/accounts"):
            next_path = "/accounts"
        return self._start_oauth(
            IDENTITY_SCOPES,
            {"purpose": "login", "next": next_path},
            {"prompt": "select_account", "include_granted_scopes": "false"},
        )

    async def connect(self, request: Request) -> Response:
        raw_alias = request.query_params.get("alias", "")
        if not self._session_email(request):
            return self._login_redirect(f"/accounts/connect?alias={quote(raw_alias)}")
        try:
            alias = normalize_alias(raw_alias)
        except StoreError as e:
            return _page("Invalid alias", f"<p class='bad'>{html.escape(str(e))}</p>", 400)
        extra = {
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
        }
        hint = request.query_params.get("login_hint")
        if not hint and (existing := self.store.get(alias)):
            hint = existing.email
        if hint:
            extra["login_hint"] = hint
        return self._start_oauth(
            IDENTITY_SCOPES + self.settings.scopes, {"purpose": "connect", "alias": alias}, extra
        )

    async def callback(self, request: Request) -> Response:
        q = request.query_params
        if q.get("error"):
            return _page("Authorization cancelled", f"<p class='bad'>Google returned: {html.escape(q['error'])}</p>"
                         "<p><a href='/accounts'>Back</a></p>", 400)
        state = self.crypto.unseal(q.get("state", ""), STATE_TTL)
        nonce_cookie = request.cookies.get(NONCE_COOKIE)
        if not state or not nonce_cookie or not secrets.compare_digest(state.get("nonce", ""), nonce_cookie):
            return _page("Invalid or expired request", "<p class='bad'>Please start again.</p>"
                         "<p><a href='/accounts'>Back</a></p>", 400)
        code = q.get("code")
        if not code:
            return _page("Missing code", "<p class='bad'>No authorization code.</p>", 400)

        purpose = state.get("purpose")
        if purpose == "login":
            resp = await self._finish_login(state, code)
        elif purpose == "connect":
            if not self._session_email(request):
                return _page("Session expired", "<p class='bad'>Sign in again.</p>"
                             "<p><a href='/accounts'>Back</a></p>", 401)
            resp = await self._finish_connect(state, code)
        else:
            return _page("Invalid request", "", 400)
        resp.delete_cookie(NONCE_COOKIE, path="/accounts")
        return resp

    async def _finish_login(self, state: dict[str, Any], code: str) -> Response:
        try:
            _, claims = await asyncio.to_thread(self._exchange, IDENTITY_SCOPES, state["cv"], code)
        except Exception as e:  # noqa: BLE001
            log.warning("Admin login token exchange failed: %s", type(e).__name__)
            return _page("Sign-in failed", "<p class='bad'>Could not verify your Google sign-in.</p>", 400)
        if not is_email_allowed(claims, self.settings.allowed_emails):
            log.warning("Rejected /accounts sign-in from an identity not in ALLOWED_EMAILS")
            return _page("Not allowed", "<p class='bad'>This Google account is not allowed to manage this server.</p>", 403)
        resp = RedirectResponse(state.get("next") or "/accounts", status_code=302)
        session = self.crypto.seal({"purpose": "session", "email": claims["email"].lower()})
        self._set_cookie(resp, SESSION_COOKIE, session, SESSION_TTL)
        return resp

    async def _finish_connect(self, state: dict[str, Any], code: str) -> Response:
        alias = state["alias"]
        requested = IDENTITY_SCOPES + self.settings.scopes
        try:
            creds, claims = await asyncio.to_thread(self._exchange, requested, state["cv"], code)
        except Exception as e:  # noqa: BLE001
            log.warning("Connect token exchange failed for alias %s: %s", alias, type(e).__name__)
            return _page("Connection failed", "<p class='bad'>Token exchange with Google failed. Try again.</p>", 400)
        email = (claims.get("email") or "").lower()
        if not email or not claims.get("email_verified"):
            return _page("Connection failed", "<p class='bad'>Google did not return a verified email.</p>", 400)
        if not creds.refresh_token:
            return _page("Connection failed", "<p class='bad'>Google did not return a refresh token. "
                         "Remove this app at myaccount.google.com/permissions and try again.</p>", 400)
        granted = sorted(set(creds.granted_scopes or creds.scopes or requested))
        missing = [s for s in self.settings.scopes if s not in granted]
        try:
            self.store.upsert(alias, email, creds.refresh_token, [s for s in granted if s in self.settings.scopes])
        except StoreError as e:
            return _page("Connection failed", f"<p class='bad'>{html.escape(str(e))}</p>", 409)
        self.backend.invalidate(alias)
        log.info("Connected account alias=%s", alias)
        warn = ""
        if missing:
            warn = ("<p class='bad'>Some permissions were not granted, so related tools will fail for this account:</p><ul>"
                    + "".join(f"<li><code>{html.escape(s.rsplit('/', 1)[-1])}</code></li>" for s in missing)
                    + "</ul><p>Reconnect and tick all boxes to grant them.</p>")
        return _page("Account connected",
                     f"<p class='ok'>Connected <b>{html.escape(email)}</b> as <b>{html.escape(alias)}</b>.</p>"
                     f"{warn}<p><a href='/accounts'>Back to accounts</a></p>")

    async def disconnect(self, request: Request) -> Response:
        if not self._session_email(request):
            return _page("Not signed in", "", 401)
        form = await request.form()
        alias = str(form.get("alias", ""))
        token = None
        try:
            token = self.store.get_refresh_token(alias)
        except Exception:  # noqa: BLE001
            token = None
        if token:
            try:
                await asyncio.to_thread(
                    requests.post, "https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=10
                )
            except requests.RequestException:
                log.warning("Token revoke call failed for alias %s", alias)
        self.store.delete(alias)
        self.backend.invalidate(alias)
        return RedirectResponse("/accounts", status_code=303)

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})
