"""MCP endpoint authentication: FastMCP's Google OAuth proxy + email allowlist."""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from .config import MCP_AUTH_CALLBACK_PATH, Settings

log = logging.getLogger(__name__)


def is_email_allowed(claims: dict | None, allowed: frozenset[str]) -> bool:
    if not claims:
        return False
    email = (claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    return bool(email) and bool(verified) and email in allowed


class AllowlistGoogleProvider(GoogleProvider):
    """GoogleProvider that only accepts identities listed in ALLOWED_EMAILS.

    The check runs on every request (not just at login), so removing an email
    from ALLOWED_EMAILS takes effect on restart without revoking tokens.
    """

    def __init__(self, *, allowed_emails: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_emails = allowed_emails

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        result = await super().load_access_token(token)
        if result is None:
            return None
        if not is_email_allowed(result.claims, self._allowed_emails):
            log.warning("Rejected MCP request from an identity not in ALLOWED_EMAILS")
            return None
        return result


def _oauth_storage(data_dir: Path, key: str) -> FernetEncryptionWrapper:
    """Persist OAuth client registrations + upstream tokens in DATA_DIR, encrypted."""
    storage_dir = data_dir / "oauth"
    storage_dir.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )
    return FernetEncryptionWrapper(
        key_value=store, fernet=Fernet(key.encode()), raise_on_decryption_error=False
    )


def build_auth_provider(settings: Settings) -> AllowlistGoogleProvider:
    return AllowlistGoogleProvider(
        allowed_emails=settings.allowed_emails,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.base_url,
        redirect_path=MCP_AUTH_CALLBACK_PATH,
        # Identity only: the MCP login never gets access to Gmail/Drive data.
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=settings.jwt_signing_key,
        client_storage=_oauth_storage(settings.data_dir, settings.token_enc_key),
        cache_ttl_seconds=300,
        extra_authorize_params={"prompt": "select_account consent"},
        allowed_client_redirect_uris=settings.mcp_allowed_redirect_uris,
    )
