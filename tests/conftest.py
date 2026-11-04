"""Fixtures de teste: injectam Supabase/NOWPayments falsas na aplicacao real."""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ENVIRONMENT"] = "test"

import pytest
import httpx
from fastapi.testclient import TestClient

from app import db as db_module
from app import nowpayments as np_module
from app.config import Settings
from app.config import get_settings
from app.deps import db_dep
from app.logging_conf import setup_logging
from app.routers import auth as auth_router
from app.security import login_limiter, user_cache
from tests.fakes import FakeNOWPayments, FakeSupabase

TEST_APP_SECRET = "t" * 64
TEST_IPN_SECRET = "ipn_secret_de_teste"

TEST_SETTINGS = Settings(
    NOWPAYMENTS_API_KEY="np_test_key_1234567890",
    NOWPAYMENTS_PUBLIC_KEY="np-public-test",
    NOWPAYMENTS_IPN_SECRET=TEST_IPN_SECRET,
    NOWPAYMENTS_BASE_URL="https://api.nowpayments.io",
    SUPABASE_URL="https://fake.supabase.co",
    SUPABASE_PUBLISHABLE_KEY="sb_publishable_teste",
    SUPABASE_SECRET_KEY="sb_secret_teste_1234567890",
    APP_SECRET_KEY=TEST_APP_SECRET,
    BASE_URL="http://localhost:8000",
    RATE_MT_PER_USDT=80,
    ENVIRONMENT="test",
)

setup_logging("test")


@pytest.fixture(autouse=True)
def _reset_state():
    user_cache.clear()
    login_limiter._hits.clear()
    yield
    user_cache.clear()
    login_limiter._hits.clear()
    db_module.set_supabase_client(None)
    np_module.set_nowpayments_client(None)
    auth_router.set_auth_transport(None)


@pytest.fixture
def settings() -> Settings:
    return TEST_SETTINGS


@pytest.fixture
def fake_sb() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def fake_np() -> FakeNOWPayments:
    return FakeNOWPayments()


@pytest.fixture
def db(fake_sb: FakeSupabase):
    return fake_sb.client()


@pytest.fixture
def app(settings: Settings, fake_sb: FakeSupabase, fake_np: FakeNOWPayments):
    from main import create_app

    db_module.set_supabase_client(fake_sb.client())
    np_module.set_nowpayments_client(fake_np.client())
    auth_router.set_auth_transport(httpx.MockTransport(fake_sb.handle))

    application = create_app(settings)
    application.dependency_overrides[db_dep] = lambda: fake_sb.client()
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded(fake_sb: FakeSupabase):
    """Cria um utilizador autenticado + perfil na Supabase falsa."""
    created = fake_sb.add_user("cliente@exemplo.mz", "password123")
    fake_sb.tables["profiles"].append(
        {
            "id": created["user"]["id"],
            "email": "cliente@exemplo.mz",
            "full_name": "Cliente",
            "role": "user",
            "balance_mt": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    return created


@pytest.fixture
def admin_user(fake_sb: FakeSupabase):
    created = fake_sb.add_user("admin@exemplo.mz", "password123")
    fake_sb.tables["profiles"].append(
        {
            "id": created["user"]["id"],
            "email": "admin@exemplo.mz",
            "full_name": "Admin",
            "role": "admin",
            "balance_mt": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    return created
