"""Regressoes de deployment: o que a Vercel exige de um projecto FastAPI."""

from __future__ import annotations

import importlib
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.deps import request_base_url
from tests.fakes import FakeNOWPayments


def test_main_exporta_app_ao_nivel_do_modulo():
    """A Vercel faz `from main import app`; sem isto o build falha com
    FASTAPI_ENTRYPOINT_NOT_FOUND."""
    import main

    importlib.reload(main)
    assert isinstance(main.app, FastAPI)


def test_vercel_json_aponta_para_main_py():
    import json

    ficheiro = pathlib.Path(__file__).resolve().parent.parent / "vercel.json"
    assert ficheiro.exists(), "vercel.json e necessario para o routing na Vercel"
    config = json.loads(ficheiro.read_text())
    assert config["builds"][0]["src"] == "main.py"
    assert config["builds"][0]["use"] == "@vercel/python"
    assert config["routes"] == [{"src": "/(.*)", "dest": "main.py"}]
    # A API da Vercel rejeita `functions` juntamente com `builds`:
    # "The `functions` property cannot be used in conjunction with the `builds` property."
    assert "functions" not in config
    assert "rewrites" not in config


def test_config_incompleta_sobe_app_com_503_em_vez_de_rebentar(monkeypatch):
    """Sem .env nem variaveis, o import de main nao pode rebentar o build."""
    import main

    motivo = "Variaveis obrigatorias em falta: SUPABASE_SECRET_KEY, APP_SECRET_KEY"

    def rebentar(*args, **kwargs):
        raise ConfigError(motivo)

    monkeypatch.setattr(main, "create_app", rebentar)
    app = main._build_default_app()

    cliente = TestClient(app, raise_server_exceptions=False)
    health = cliente.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "config_missing"
    assert "SUPABASE_SECRET_KEY" in health.json()["detail"]  # nomes, nunca valores

    outra = cliente.get("/api/payments")
    assert outra.status_code == 503
    assert "detail" in outra.json()


def test_url_base_deriva_do_pedido_quando_base_url_vazia(settings):
    vazio = settings.model_copy(update={"BASE_URL": ""})

    class Pedido:
        headers = {"x-forwarded-host": "rabudas.vercel.app", "x-forwarded-proto": "https"}
        url = type("U", (), {"scheme": "http"})()
        base_url = "http://testserver/"

    assert request_base_url(Pedido(), vazio) == "https://rabudas.vercel.app"


def test_url_base_usa_base_url_quando_definida(settings):
    class Pedido:
        headers = {"x-forwarded-host": "rabudas.vercel.app", "x-forwarded-proto": "https"}
        url = type("U", (), {"scheme": "http"})()
        base_url = "http://testserver/"

    definido = settings.model_copy(update={"BASE_URL": "https://exemplo.mz/"})
    assert request_base_url(Pedido(), definido) == "https://exemplo.mz"


def test_callbacks_da_nowpayments_usam_o_url_publico(db, fake_np, fake_sb, settings):
    """Sem isto, em producao o IPN ficava a apontar para localhost."""
    from app.schemas import PaymentCreateRequest
    from app.services import create_payment

    fake_sb.tables["profiles"].append(
        {
            "id": "u-1",
            "email": "a@b.mz",
            "full_name": "",
            "role": "user",
            "balance_mt": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    vazio = settings.model_copy(update={"BASE_URL": ""})
    create_payment(
        db,
        fake_np.client(),
        vazio,
        user_id="u-1",
        email="a@b.mz",
        request=PaymentCreateRequest(amount_mt=1000),
        base_url="https://rabudas.vercel.app",
    )
    pedido = fake_np.created[0]
    assert pedido["ipn_callback_url"] == "https://rabudas.vercel.app/api/webhooks/nowpayments"
    assert pedido["success_url"].startswith("https://rabudas.vercel.app/dashboard.html")


def test_cookie_secure_activa_se_em_producao(settings):
    from app.deps import cookie_secure

    class Pedido:
        headers = {}

    producao = settings.model_copy(update={"ENVIRONMENT": "production", "BASE_URL": ""})
    assert cookie_secure(Pedido(), producao) is True

    https = settings.model_copy(update={"ENVIRONMENT": "development", "BASE_URL": "https://x.mz"})
    assert cookie_secure(Pedido(), https) is True

    class PedidoHttp:
        headers = {"x-forwarded-proto": "http"}

    local = settings.model_copy(update={"ENVIRONMENT": "development", "BASE_URL": ""})
    assert cookie_secure(PedidoHttp(), local) is False


# --------------------------------------------------------------------------- #
# Regressao: o header `apikey` nunca pode levar o JWT do utilizador
# --------------------------------------------------------------------------- #
def test_get_user_envia_a_api_key_do_projecto_e_nao_o_jwt(fake_sb):
    """Bug real: o apikey levava o access token do utilizador e o GoTrue
    respondia 401, partindo o registo (nada em `profiles`) e o login
    ("sessao expirada")."""
    db = fake_sb.client()
    criado = fake_sb.add_user("cliente@exemplo.mz")

    user = db.get_user(criado["access_token"])

    assert user["id"] == criado["user"]["id"]
    assert user["email"] == "cliente@exemplo.mz"

    path, apikey, authorization = fake_sb.auth_requests[-1]
    assert path.endswith("/user")
    assert apikey == "sb_secret_teste", "o apikey tem de ser uma API key do projecto"
    assert apikey != criado["access_token"]
    assert authorization == f"Bearer {criado['access_token']}"


def test_signup_e_login_usam_a_chave_publica_no_apikey(fake_sb):
    """O registo/login do lado do servidor usam a chave PUBLICA, nunca a secreta."""
    from app.config import Settings
    from app.routers.auth import _auth_call, set_auth_transport

    import httpx

    cfg = Settings(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_PUBLISHABLE_KEY="sb_publishable_teste",
        SUPABASE_SECRET_KEY="sb_secret_teste",
        NOWPAYMENTS_API_KEY="np",
        APP_SECRET_KEY="a" * 64,
    )
    set_auth_transport(httpx.MockTransport(fake_sb.handle))
    _auth_call(cfg, "POST", "/signup", json_body={"email": "novo@x.mz", "password": "password123"})

    path, apikey, _ = fake_sb.auth_requests[-1]
    assert path.endswith("/signup")
    assert apikey == "sb_publishable_teste"


def test_apikey_invalido_levanta_erro_de_supabase(fake_sb):
    import httpx

    from app.db import SupabaseClient, SupabaseError

    db = SupabaseClient(
        "https://fake.supabase.co", "chave-que-nao-e-api-key", transport=httpx.MockTransport(fake_sb.handle)
    )
    with pytest.raises(SupabaseError) as erro:
        db.get_user("token-qualquer")
    assert erro.value.status_code == 401


def test_tabela_profiles_em_falta_da_erro_claro_e_nao_sessao_expirada(fake_sb, settings):
    """Se o schema nao estiver corrido, o utilizador tem de ver isso -
    nao um "sessao expirada" que nao explica nada."""
    from fastapi.testclient import TestClient

    from app import db as db_module
    from app import nowpayments as np_module
    from app.config import get_settings
    from app.deps import db_dep
    from main import create_app

    fake_sb.add_user("cliente@exemplo.mz")
    del fake_sb.tables["profiles"]

    db_module.set_supabase_client(fake_sb.client())
    np_module.set_nowpayments_client(FakeNOWPayments().client())
    app = create_app(settings)
    app.dependency_overrides[db_dep] = lambda: fake_sb.client()
    app.dependency_overrides[get_settings] = lambda: settings

    cliente = TestClient(app, raise_server_exceptions=False)
    resposta = cliente.post(
        "/api/auth/session",
        json={"access_token": next(iter(fake_sb.users))},
    )
    assert resposta.status_code == 503, resposta.text
    assert "schema.sql" in resposta.json()["detail"]
    db_module.set_supabase_client(None)
