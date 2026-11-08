"""Shared application context passed to tool implementations."""

from __future__ import annotations

from dataclasses import dataclass

from .accounts import resolve_accounts, resolve_one
from .config import Settings
from .google_auth import ServiceRunner
from .store import Account, AccountStore


@dataclass
class AppContext:
    settings: Settings
    store: AccountStore
    google: ServiceRunner

    @property
    def accounts_page_url(self) -> str:
        return f"{self.settings.base_url}/accounts"

    def resolve(self, account: str, allow_all: bool = True) -> list[Account]:
        return resolve_accounts(
            self.store, account, allow_all=allow_all, connect_hint_url=self.accounts_page_url
        )

    def resolve_one(self, account: str) -> Account:
        return resolve_one(self.store, account, connect_hint_url=self.accounts_page_url)
