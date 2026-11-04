"""Testes de ponta a ponta da API (HTTP) com servicos externos falsos."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.nowpayments import sign_ipn_payload
from tests.conftest import TEST_IPN_SECRET

CLIENTE = "cliente@exemplo.mz"
ADMIN = "admin@exemplo.mz"
PASSWORD = "password123"


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def ipn_headers(payload: dict) -> dict:
    return {"x-nowpayments-sig": sign_ipn_payload(payload, TEST_IPN_SECRET)}


# --------------------------------------------------------------------------- #
# Endpoints publicos
# --------------------------------------------------------------------------- #
def test_health(client):
    resposta = client.get("/health")
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "ok"
    assert corpo["rate_mt_per_usdt"] == 80


def test_config_publico_nao_vaza_segredos(client, settings):
    corpo = client.get("/api/config").json()
    texto = str(corpo)
    assert corpo["supabasePublishableKey"] == settings.SUPABASE_PUBLISHABLE_KEY
    assert settings.SUPABASE_SECRET_KEY.get_secret_value() not in texto
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value() not in texto
    assert settings.APP_SECRET_KEY.get_secret_value() not in texto


def test_pagina_inicial_e_servida(client):
    resposta = client.get("/")
    assert resposta.status_code == 200
    assert "LubraCrypto" in resposta.text


def test_quote_publico(client):
    corpo = client.get("/api/payments/quote?amount_mt=1000").json()
    assert corpo["amount_usdt"] == "12.50"
    assert corpo["rate_mt_per_usdt"] == 80


def test_endpoints_protegidos_exigem_sessao(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/payments").status_code == 401
    assert client.get("/api/payouts").status_code == 401
    assert client.get("/api/admin/stats").status_code == 401


# --------------------------------------------------------------------------- #
# Autenticacao
# --------------------------------------------------------------------------- #
def test_signup_cria_sessao_e_perfil(client, fake_sb):
    resposta = client.post(
        "/api/auth/signup",
        json={"email": "novo@exemplo.mz", "password": PASSWORD, "full_name": "Novo"},
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["email"] == "novo@exemplo.mz"
    assert corpo["role"] == "user"
    assert "rabudas_session" in resposta.cookies

    perfis = fake_sb.tables["profiles"]
    assert len(perfis) == 1
    assert perfis[0]["email"] == "novo@exemplo.mz"

    eu = client.get("/api/auth/me")
    assert eu.status_code == 200
    assert eu.json()["email"] == "novo@exemplo.mz"


def test_login_valido_e_invalido(client, seeded, fake_sb):
    assert login(client, CLIENTE, "palavra-errada").status_code == 401
    registos = [r for r in fake_sb.tables["login_logs"] if not r["success"]]
    assert registos and registos[0]["email"] == CLIENTE

    ok = login(client, CLIENTE)
    assert ok.status_code == 200, ok.text
    assert client.get("/api/auth/me").json()["email"] == CLIENTE


def test_login_logs_regista_ip_e_user_agent(client, seeded, fake_sb):
    login(client, CLIENTE)
    registo = fake_sb.tables["login_logs"][-1]
    assert registo["success"] is True
    assert registo["reason"] == "login"
    assert "user_agent" in registo


def test_rate_limit_de_login(client, seeded):
    for _ in range(5):
        assert login(client, CLIENTE, "errada").status_code == 401
    resposta = login(client, CLIENTE, "errada")
    assert resposta.status_code == 429
    assert "espera" in resposta.json()["detail"].lower()


def test_logout_termina_sessao(client, seeded):
    login(client, CLIENTE)
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_troca_de_token_do_navegador(client, seeded):
    resposta = client.post("/api/auth/session", json={"access_token": seeded["access_token"]})
    assert resposta.status_code == 200, resposta.text
    assert client.get("/api/auth/me").json()["email"] == CLIENTE
    assert client.post("/api/auth/session", json={"access_token": "token-invalido"}).status_code == 401


# --------------------------------------------------------------------------- #
# Depositos + IPN
# --------------------------------------------------------------------------- #
def test_fluxo_deposito_ipn_credita_saldo(client, seeded):
    login(client, CLIENTE)

    criado = client.post("/api/payments", json={"amount_mt": 1000, "pay_currency": "usdttrc20"})
    assert criado.status_code == 201, criado.text
    pagamento = criado.json()
    assert pagamento["amount_usdt"] == "12.50"
    assert pagamento["pay_address"].startswith("T")
    assert pagamento["status"] == "pending"

    payload = {
        "order_id": pagamento["order_id"],
        "payment_status": "finished",
        "actually_paid": 12.5,
        "outcome_amount": 12.5,
        "outcome_currency": "usdttrc20",
    }
    ipn = client.post("/api/webhooks/nowpayments", json=payload, headers=ipn_headers(payload))
    assert ipn.status_code == 200, ipn.text
    assert ipn.json() == {"ok": True, "status": "paid"}

    eu = client.get("/api/auth/me").json()
    assert eu["balance_mt"] == "1000.00"

    meus = client.get("/api/payments").json()
    assert meus[0]["status"] == "paid"
    assert meus[0]["credited_mt"] == "1000.00"


def test_deposito_minimo_validado(client, seeded):
    login(client, CLIENTE)
    resposta = client.post("/api/payments", json={"amount_mt": 10})
    assert resposta.status_code == 422
    assert "minimo" in str(resposta.json()).lower()


def test_deposito_minimo_e_de_10_dolares_ou_seja_800_mt(client, seeded, fake_np):
    """O minino e definido em USD: 10 USD a 80 MT/USDT sao 800 MT.

    Regressao: se alguem voltar a comparar o valor em MT com o numero 10, o
    deposito de 100 MT (que sao 1.25 USD) passaria a ser aceite.
    """
    login(client, CLIENTE)

    # 100 MT = 1.25 USD -> abaixo do minimo de 10 USD.
    baixo = client.post("/api/payments", json={"amount_mt": 100})
    assert baixo.status_code == 422, baixo.text
    detalhe = baixo.json()["detail"].lower()
    assert "minimo" in detalhe
    assert "10" in detalhe  # diz o minimo em USD
    assert "800" in detalhe  # e o equivalente em MT

    # 800 MT = 10.00 USD -> aceite.
    ok = client.post("/api/payments", json={"amount_mt": 800})
    assert ok.status_code == 201, ok.text
    assert ok.json()["amount_usdt"] == "10.00"


def test_minimo_configuravel_pelo_admin(client, seeded, admin_user, fake_sb):
    """O admin muda o minimo em USD e a validacao acompanha."""
    login(client, ADMIN)
    resposta = client.put("/api/admin/min-deposit", json={"min_deposit_usd": 20})
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["min_deposit_usd"] == 20
    assert resposta.json()["min_deposit_mt"] == 1600.0

    # 800 MT (10 USD) deixa de passar; 1600 MT (20 USD) passa.
    assert client.post("/api/payments", json={"amount_mt": 800}).status_code == 422
    assert client.post("/api/payments", json={"amount_mt": 1600}).status_code == 201


def test_minimo_do_admin_aparece_em_api_config(client, seeded, admin_user):
    login(client, ADMIN)
    resposta = client.put("/api/admin/min-deposit", json={"min_deposit_usd": 25})
    assert resposta.status_code == 200, resposta.text
    config = client.get("/api/config").json()
    # minDepositUsd continua a ser o valor do .env (config estatica); o que o
    # admin define na base de dados chega ao browser em minDepositMt.
    assert config["minDepositUsd"] == 10.0
    assert config["minDepositMt"] == 2000.0


def test_ipn_com_assinatura_invalida_e_recusado(client, seeded, fake_sb):
    login(client, CLIENTE)
    pagamento = client.post("/api/payments", json={"amount_mt": 1000}).json()
    payload = {"order_id": pagamento["order_id"], "payment_status": "finished", "actually_paid": 12.5}
    resposta = client.post(
        "/api/webhooks/nowpayments",
        json=payload,
        headers={"x-nowpayments-sig": "assinatura-falsa"},
    )
    assert resposta.status_code == 401
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 0


def test_ipn_sem_segredo_configurado_devolve_503(client, settings, fake_sb, fake_np):
    from app import db as db_module
    from app import nowpayments as np_module
    from main import create_app

    sem_ipn = settings.model_copy(update={"NOWPAYMENTS_IPN_SECRET": type(settings.NOWPAYMENTS_IPN_SECRET)("")})
    db_module.set_supabase_client(fake_sb.client())
    np_module.set_nowpayments_client(fake_np.client())
    app = create_app(sem_ipn)
    from app.config import get_settings
    from app.deps import db_dep

    app.dependency_overrides[db_dep] = lambda: fake_sb.client()
    app.dependency_overrides[get_settings] = lambda: sem_ipn
    outro = TestClient(app, raise_server_exceptions=False)

    assert outro.get("/health").json()["ipn_enabled"] is False
    payload = {"order_id": "RBD-X", "payment_status": "finished"}
    resposta = outro.post(
        "/api/webhooks/nowpayments", json=payload, headers=ipn_headers(payload)
    )
    assert resposta.status_code == 503


# --------------------------------------------------------------------------- #
# Levantamentos
# --------------------------------------------------------------------------- #
def test_payout_exige_saldo_e_valida_telefone(client, seeded):
    login(client, CLIENTE)
    sem_saldo = client.post(
        "/api/payouts", json={"amount_mt": 500, "method": "mpesa", "phone": "841234567"}
    )
    assert sem_saldo.status_code == 409
    assert "saldo" in sem_saldo.json()["detail"].lower()

    telefone = client.post(
        "/api/payouts", json={"amount_mt": 500, "method": "mpesa", "phone": "123"}
    )
    assert telefone.status_code == 422


def test_fluxo_payout_aprovado_pelo_admin(client, seeded, admin_user, fake_sb):
    fake_sb.tables["profiles"][0]["balance_mt"] = 1000

    login(client, CLIENTE)
    pedido = client.post(
        "/api/payouts",
        json={"amount_mt": 400, "method": "mpesa", "phone": "841234567", "holder_name": "Cliente"},
    )
    assert pedido.status_code == 201, pedido.text
    payout = pedido.json()
    assert payout["status"] == "pending"
    assert payout["phone_masked"] == "25884***67"
    assert client.get("/api/auth/me").json()["balance_mt"] == "600.00"

    admin = TestClient(client.app, raise_server_exceptions=False)
    assert login(admin, ADMIN).status_code == 200

    pendentes = admin.get("/api/admin/payouts?status=pending").json()
    assert len(pendentes) == 1

    decisao = admin.post(
        f"/api/admin/payouts/{payout['id']}/decision",
        json={"decision": "approve", "note": "enviado por M-Pesa"},
    )
    assert decisao.status_code == 200, decisao.text
    assert decisao.json()["status"] == "approved"

    estatisticas = admin.get("/api/admin/stats").json()
    assert estatisticas["payouts_pending"] == 0
    assert estatisticas["payouts_approved"] == 1
    assert estatisticas["total_paid_out_mt"] == 400.0

    assert client.get("/api/auth/me").json()["balance_mt"] == "600.00"


def test_payout_rejeitado_devolve_saldo_ao_utilizador(client, seeded, admin_user, fake_sb):
    fake_sb.tables["profiles"][0]["balance_mt"] = 1000
    login(client, CLIENTE)
    payout = client.post(
        "/api/payouts", json={"amount_mt": 300, "method": "emola", "phone": "861234567"}
    ).json()

    admin = TestClient(client.app, raise_server_exceptions=False)
    login(admin, ADMIN)
    decisao = admin.post(
        f"/api/admin/payouts/{payout['id']}/decision", json={"decision": "reject", "note": "numero errado"}
    )
    assert decisao.json()["status"] == "rejected"
    assert client.get("/api/auth/me").json()["balance_mt"] == "1000.00"


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def test_admin_recusa_utilizador_normal(client, seeded):
    login(client, CLIENTE)
    assert client.get("/api/admin/stats").status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_admin_ve_pagamentos_logs_e_saude(client, seeded, admin_user):
    admin = TestClient(client.app, raise_server_exceptions=False)
    login(admin, ADMIN)

    utilizadores = admin.get("/api/admin/users").json()
    assert {u["role"] for u in utilizadores} == {"admin", "user"}

    saude = admin.get("/api/admin/health-check").json()
    assert saude["supabase"] == "ok"
    assert saude["nowpayments"] == "ok"
    assert saude["ipn"] == "ok"
    assert saude["ipn_callback_url"].endswith("/api/webhooks/nowpayments")

    assert admin.get("/api/admin/payments").status_code == 200
    assert admin.get("/api/admin/login-logs").status_code == 200
    assert len(admin.get("/api/admin/login-logs").json()) >= 1


def test_admin_altera_taxa_que_afecta_novos_depositos(client, seeded, admin_user, fake_sb):
    admin = TestClient(client.app, raise_server_exceptions=False)
    login(admin, ADMIN)
    assert admin.put("/api/admin/rate", json={"rate_mt_per_usdt": 85}).status_code == 200
    assert float(fake_sb.tables["settings"][0]["value"]) == 85.0

    assert client.get("/api/payments/quote?amount_mt=850").json()["amount_usdt"] == "10.00"


def test_admin_promove_utilizador(client, seeded, admin_user, fake_sb):
    admin = TestClient(client.app, raise_server_exceptions=False)
    login(admin, ADMIN)
    alvo = fake_sb.tables["profiles"][0]
    resposta = admin.post(f"/api/admin/users/{alvo['id']}/role?role=admin")
    assert resposta.status_code == 200
    assert fake_sb.tables["profiles"][0]["role"] == "admin"


def test_documento_openapi_exposto(client):
    assert client.get("/docs").status_code == 200
    esquema = client.get("/openapi.json").json()
    assert "/api/payments" in esquema["paths"]


def test_erro_interno_devolve_500_sem_detalhes(client, seeded, monkeypatch):
    from app.routers import payments as payments_router

    login(client, CLIENTE)

    def rebentar(*args, **kwargs):
        raise RuntimeError("segredo interno nao deve sair")

    monkeypatch.setattr(payments_router, "list_payments", rebentar)
    resposta = client.get("/api/payments")
    assert resposta.status_code == 500
    assert resposta.json() == {"detail": "Erro interno do servidor."}
    assert "segredo interno" not in resposta.text


def test_quote_funciona_mesmo_sem_base_de_dados(settings, fake_np):
    """O endpoint publico de cotacao nao pode rebentar se a Supabase estiver em baixo."""
    import httpx

    from app import db as db_module
    from app import nowpayments as np_module
    from app.config import get_settings
    from app.deps import db_dep
    from app.db import SupabaseClient
    from main import create_app

    def rebentar(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede", request=request)

    db_module.set_supabase_client(
        SupabaseClient("https://fake.supabase.co", "sb_secret_teste", transport=httpx.MockTransport(rebentar))
    )
    np_module.set_nowpayments_client(fake_np.client())
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    sem_rede = TestClient(app, raise_server_exceptions=False)
    resposta = sem_rede.get("/api/payments/quote?amount_mt=8000")
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["amount_usdt"] == "100.00"
    assert corpo["rate_mt_per_usdt"] == 80
    db_module.set_supabase_client(None)
