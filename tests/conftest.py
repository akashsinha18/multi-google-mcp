from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from multi_google_mcp.config import Settings
from multi_google_mcp.context import AppContext
from multi_google_mcp.crypto import Crypto
from multi_google_mcp.store import AccountStore

from .fakes import FakeRunner


@pytest.fixture
def settings(tmp_path):
    return Settings(
        google_client_id="cid.apps.googleusercontent.com",
        google_client_secret="secret",
        base_url="https://mcp.example.com",
        token_enc_key=Fernet.generate_key().decode(),
        allowed_emails=frozenset({"me@example.com"}),
        data_dir=tmp_path,
    )


@pytest.fixture
def crypto(settings):
    return Crypto(settings.token_enc_key)


@pytest.fixture
def store(settings, crypto):
    s = AccountStore(settings.db_path, crypto)
    s.upsert("personal", "Me@Gmail.com", "rt-personal", ["scope-a"])
    s.upsert("work", "me@corp.com", "rt-work", ["scope-a"])
    return s


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def ctx(settings, store, runner):
    return AppContext(settings=settings, store=store, google=runner)
