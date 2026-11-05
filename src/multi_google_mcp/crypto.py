"""Fernet helpers: encrypt refresh tokens at rest and sign short-lived blobs."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class Crypto:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as e:
            raise ValueError(
                "TOKEN_ENC_KEY is not a valid Fernet key; generate one with "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            ) from e

    @property
    def fernet(self) -> Fernet:
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def seal(self, payload: dict[str, Any]) -> str:
        """Encrypt + authenticate a JSON payload (used for OAuth state / cookies)."""
        return self.encrypt(json.dumps(payload, separators=(",", ":")))

    def unseal(self, token: str, max_age_seconds: int) -> dict[str, Any] | None:
        try:
            raw = self._fernet.decrypt(token.encode(), ttl=max_age_seconds)
        except (InvalidToken, ValueError, TypeError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
