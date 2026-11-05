"""Configuration, read exclusively from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Google API scopes requested when connecting a data account.
# Trim this list to reduce access; tools that need a removed scope will fail
# with a clear Google "insufficient permissions" error for that account.
# ---------------------------------------------------------------------------
GOOGLE_API_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

# Identity scopes, always added so we can verify the connected account's email.
IDENTITY_SCOPES: list[str] = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

MCP_PATH = "/mcp"
MCP_AUTH_CALLBACK_PATH = "/auth/callback"  # FastMCP GoogleProvider callback
ACCOUNTS_CALLBACK_PATH = "/accounts/callback"  # data-account connect callback


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    google_client_id: str
    google_client_secret: str
    base_url: str  # public URL, no trailing slash, e.g. https://mcp.example.com
    token_enc_key: str  # Fernet key (urlsafe base64, 32 bytes)
    allowed_emails: frozenset[str]
    data_dir: Path = Path("./data")
    jwt_signing_key: str | None = None
    port: int = 8080
    mcp_allowed_redirect_uris: list[str] | None = None
    sqlite_journal_mode: str = "WAL"
    scopes: list[str] = field(default_factory=lambda: list(GOOGLE_API_SCOPES))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "accounts.db"

    @property
    def accounts_redirect_uri(self) -> str:
        return self.base_url + ACCOUNTS_CALLBACK_PATH

    def connect_url(self, alias: str) -> str:
        from urllib.parse import quote

        return f"{self.base_url}/accounts/connect?alias={quote(alias)}"

    @classmethod
    def from_env(cls) -> "Settings":
        allowed = frozenset(
            e.strip().lower()
            for e in _require("ALLOWED_EMAILS").split(",")
            if e.strip()
        )
        if not allowed:
            raise ConfigError("ALLOWED_EMAILS must list at least one email")
        return cls(
            google_client_id=_require("GOOGLE_CLIENT_ID"),
            google_client_secret=_require("GOOGLE_CLIENT_SECRET"),
            base_url=_require("BASE_URL").rstrip("/"),
            token_enc_key=_require("TOKEN_ENC_KEY"),
            allowed_emails=allowed,
            data_dir=Path(os.environ.get("DATA_DIR", "./data")),
            jwt_signing_key=os.environ.get("JWT_SIGNING_KEY") or None,
            port=int(os.environ.get("PORT", "8080")),
            mcp_allowed_redirect_uris=[
                u.strip() for u in os.environ["MCP_ALLOWED_REDIRECT_URIS"].split(",") if u.strip()
            ]
            if os.environ.get("MCP_ALLOWED_REDIRECT_URIS")
            else None,
            sqlite_journal_mode=os.environ.get("SQLITE_JOURNAL_MODE", "WAL").upper(),
        )
