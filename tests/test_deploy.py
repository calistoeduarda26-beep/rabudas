"""Regressoes de deployment: o que a Vercel exige de um projecto FastAPI."""

from __future__ import annotations

import importlib
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.deps import request_base_url


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
