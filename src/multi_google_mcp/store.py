"""SQLite-backed storage of connected Google accounts.

Refresh tokens are stored Fernet-encrypted; they are never returned by
`list_accounts` and never logged.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .crypto import Crypto

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

STATUS_CONNECTED = "connected"
STATUS_REVOKED = "revoked"


class StoreError(ValueError):
    pass


@dataclass(frozen=True)
class Account:
    alias: str
    email: str
    status: str
    scopes: tuple[str, ...] = ()
    last_error: str | None = None

    @property
    def ref(self) -> dict[str, str]:
        """Compact identifier included in every tool result."""
        return {"alias": self.alias, "email": self.email}


def normalize_alias(alias: str) -> str:
    a = (alias or "").strip().lower()
    if not ALIAS_RE.match(a) or a == "all":
        raise StoreError(
            "Alias must be 1-32 chars of a-z, 0-9, '-' or '_', start with a "
            "letter or digit, and not be 'all'."
        )
    return a


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    alias          TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    refresh_token  TEXT NOT NULL,   -- Fernet ciphertext
    scopes         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'connected',
    last_error     TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
"""


class AccountStore:
    def __init__(self, db_path: Path | str, crypto: Crypto, journal_mode: str = "WAL"):
        self._crypto = crypto
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if journal_mode not in {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"}:
                raise StoreError(f"Unsupported SQLITE_JOURNAL_MODE {journal_mode!r}")
            self._conn.execute(f"PRAGMA journal_mode={journal_mode}")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            alias=row["alias"],
            email=row["email"],
            status=row["status"],
            scopes=tuple(s for s in row["scopes"].split() if s),
            last_error=row["last_error"],
        )

    def list(self) -> list[Account]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alias, email, status, scopes, last_error FROM accounts ORDER BY alias"
            ).fetchall()
        return [self._row_to_account(r) for r in rows]

    def get(self, alias: str) -> Account | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT alias, email, status, scopes, last_error FROM accounts WHERE alias = ?",
                (alias,),
            ).fetchone()
        return self._row_to_account(row) if row else None

    def get_refresh_token(self, alias: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT refresh_token FROM accounts WHERE alias = ?", (alias,)
            ).fetchone()
        return self._crypto.decrypt(row["refresh_token"]) if row else None

    def upsert(self, alias: str, email: str, refresh_token: str, scopes: list[str]) -> Account:
        alias = normalize_alias(alias)
        email = email.strip().lower()
        now = time.time()
        enc = self._crypto.encrypt(refresh_token)
        with self._lock:
            other = self._conn.execute(
                "SELECT alias FROM accounts WHERE email = ? AND alias != ?", (email, alias)
            ).fetchone()
            if other:
                raise StoreError(
                    f"{email} is already connected as alias '{other['alias']}'. "
                    f"Reconnect using that alias, or disconnect it first."
                )
            self._conn.execute(
                """
                INSERT INTO accounts (alias, email, refresh_token, scopes, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'connected', NULL, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    email = excluded.email,
                    refresh_token = excluded.refresh_token,
                    scopes = excluded.scopes,
                    status = 'connected',
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (alias, email, enc, " ".join(scopes), now, now),
            )
            self._conn.commit()
        return Account(alias=alias, email=email, status=STATUS_CONNECTED, scopes=tuple(scopes))

    def mark_status(self, alias: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET status = ?, last_error = ?, updated_at = ? WHERE alias = ?",
                (status, error, time.time(), alias),
            )
            self._conn.commit()

    def delete(self, alias: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM accounts WHERE alias = ?", (alias,))
            self._conn.commit()
        return cur.rowcount > 0
