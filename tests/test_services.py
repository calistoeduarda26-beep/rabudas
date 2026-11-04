"""Regras de negocio exercitando `app/services.py` com Supabase/NOWPayments falsas."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas import PaymentCreateRequest, PayoutCreateRequest
from app.services import (
    BusinessError,
    create_payment,
    create_payout,
    credit_balance,
    debit_balance,
    decide_payout,
    ensure_profile,
    get_rate,
    handle_ipn,
    list_payments,
    list_payouts,
    sync_payment,
)

USER_ID = "11111111-1111-1111-1111-111111111111"
EMAIL = "cliente@exemplo.mz"


def seed_profile(fake_sb, balance: float = 0) -> None:
    fake_sb.tables["profiles"].append(
        {
            "id": USER_ID,
            "email": EMAIL,
            "full_name": "Cliente",
            "role": "user",
            "balance_mt": balance,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )


def payment_request(amount: float = 1000) -> PaymentCreateRequest:
    return PaymentCreateRequest(amount_mt=amount, pay_currency="usdttrc20")


def payout_request(amount: float, phone: str = "841234567") -> PayoutCreateRequest:
    return PayoutCreateRequest(amount_mt=amount, method="mpesa", phone=phone, holder_name="Cliente")


# --------------------------------------------------------------------------- #
# Taxa e perfis
# --------------------------------------------------------------------------- #
def test_taxa_vem_da_tabela_settings(db, fake_sb, settings):
    assert get_rate(db, settings) == Decimal("80")
    fake_sb.tables["settings"][0]["value"] = "85.5"
    assert get_rate(db, settings) == Decimal("85.5")


def test_ensure_profile_cria_uma_vez(db, fake_sb):
    perfil = ensure_profile(db, USER_ID, EMAIL, "Cliente")
    assert perfil["id"] == USER_ID
    assert len(fake_sb.tables["profiles"]) == 1
    ensure_profile(db, USER_ID, EMAIL, "Cliente")
    assert len(fake_sb.tables["profiles"]) == 1


# --------------------------------------------------------------------------- #
# Depositos
# --------------------------------------------------------------------------- #
def test_create_payment_converte_mt_para_usdt_e_chama_nowpayments(db, fake_np, fake_sb, settings):
    seed_profile(fake_sb)
    linha = create_payment(
        db, fake_np.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(1000)
    )
    assert linha["amount_mt"] == "1000.00"
    assert linha["amount_usdt"] == "12.50"
    assert linha["pay_address"] == "TQn9Y2khDD95J42FQtQTdwBMRk1XfK6Z9p"

    pedido = fake_np.created[0]
    assert pedido["price_amount"] == 12.5
    assert pedido["price_currency"] == "usd"
    assert pedido["pay_currency"] == "usdttrc20"
    assert pedido["order_id"] == linha["order_id"]
    assert pedido["ipn_callback_url"] == "http://localhost:8000/api/webhooks/nowpayments"
    assert pedido["success_url"].startswith("http://localhost:8000/dashboard.html")

    guardado = fake_sb.tables["payments"][0]
    assert guardado["status"] == "pending"
    assert guardado["np_payment_id"] == "9000001"


def test_create_payment_regista_falha_da_api(db, fake_sb, settings):
    from tests.fakes import FakeNOWPayments

    seed_profile(fake_sb)
    falso = FakeNOWPayments(fail_create=True)
    with pytest.raises(BusinessError) as erro:
        create_payment(
            db, falso.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(1000)
        )
    assert erro.value.code == "np_error"
    assert fake_sb.tables["payments"][0]["status"] == "failed"


def test_ipn_finished_credita_saldo_uma_vez(db, fake_np, fake_sb, settings):
    seed_profile(fake_sb)
    linha = create_payment(
        db, fake_np.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(1000)
    )
    payload = {
        "order_id": linha["order_id"],
        "payment_status": "finished",
        "actually_paid": 12.5,
        "outcome_amount": 12.5,
        "outcome_currency": "usdttrc20",
    }

    actualizado = handle_ipn(db, payload)
    assert actualizado["status"] == "paid"
    assert actualizado["credited_mt"] == "1000.00"
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 1000

    # segunda notificacao nao credita duas vezes
    handle_ipn(db, payload)
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 1000
    assert fake_sb.tables["payments"][0]["credited_mt"] == "1000.00"


def test_ipn_waiting_nao_credita(db, fake_np, fake_sb, settings):
    seed_profile(fake_sb)
    linha = create_payment(
        db, fake_np.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(800)
    )
    actualizado = handle_ipn(db, {"order_id": linha["order_id"], "payment_status": "waiting"})
    assert actualizado["status"] == "pending"
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 0


def test_ipn_de_ordem_desconhecida(db, fake_sb):
    seed_profile(fake_sb)
    with pytest.raises(BusinessError) as erro:
        handle_ipn(db, {"order_id": "RBD-NAO-EXISTE", "payment_status": "finished"})
    assert erro.value.code == "unknown_order"


def test_sync_payment_vai_a_nowpayments_e_credita(db, fake_sb, settings):
    from tests.fakes import FakeNOWPayments

    seed_profile(fake_sb)
    falso = FakeNOWPayments(status="finished")
    linha = create_payment(
        db, falso.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(2000)
    )
    actualizado = sync_payment(db, falso.client(), linha["id"])
    assert actualizado["status"] == "paid"
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 2000
    assert falso.fetched == ["9000001"]


def test_list_payments_filtra_por_utilizador(db, fake_np, fake_sb, settings):
    seed_profile(fake_sb)
    create_payment(db, fake_np.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(1000))
    outro = "22222222-2222-2222-2222-222222222222"
    create_payment(db, fake_np.client(), settings, user_id=outro, email="outro@x.mz", request=payment_request(900))
    meus = list_payments(db, user_id=USER_ID)
    assert len(meus) == 1
    assert meus[0]["amount_mt"] == "1000.00"


# --------------------------------------------------------------------------- #
# Levantamentos
# --------------------------------------------------------------------------- #
def test_payout_sem_saldo_e_recusado(db, fake_sb, settings):
    seed_profile(fake_sb, balance=100)
    with pytest.raises(BusinessError) as erro:
        create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(500))
    assert erro.value.code == "insufficient_funds"
    assert fake_sb.tables["payouts"] == []


def test_payout_debita_saldo_e_cria_pedido(db, fake_sb, settings):
    seed_profile(fake_sb, balance=1000)
    linha = create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(400))
    assert linha["status"] == "pending"
    assert float(linha["amount_mt"]) == 400.0
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 600


def test_rejeicao_devolve_o_saldo(db, fake_sb, settings):
    seed_profile(fake_sb, balance=1000)
    linha = create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(400))
    decidido = decide_payout(db, payout_id=linha["id"], decision="reject", note="numero invalido", admin_id="admin-1")
    assert decidido["status"] == "rejected"
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 1000


def test_aprovacao_mantem_debito(db, fake_sb, settings):
    seed_profile(fake_sb, balance=1000)
    linha = create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(400))
    decidido = decide_payout(db, payout_id=linha["id"], decision="approve", note="pago", admin_id="admin-1")
    assert decidido["status"] == "approved"
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 600


def test_decidir_duas_vezes_e_recusado(db, fake_sb, settings):
    seed_profile(fake_sb, balance=1000)
    linha = create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(400))
    decide_payout(db, payout_id=linha["id"], decision="approve", note="", admin_id="admin-1")
    with pytest.raises(BusinessError) as erro:
        decide_payout(db, payout_id=linha["id"], decision="reject", note="", admin_id="admin-1")
    assert erro.value.code in {"already_decided", "settle_failed"}


def test_list_payouts_com_email_do_perfil(db, fake_sb, settings):
    seed_profile(fake_sb, balance=1000)
    create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(300))
    linhas = list_payouts(db, user_id=USER_ID)
    assert len(linhas) == 1


# --------------------------------------------------------------------------- #
# Sem funcoes SQL (fallback de actualizacao condicional)
# --------------------------------------------------------------------------- #
def test_creditos_e_debitos_funcionam_sem_funcoes_sql(fake_sb, settings):
    fake_sb.rpc_enabled = False
    db = fake_sb.client()
    seed_profile(fake_sb, balance=500)

    novo = credit_balance(db, USER_ID, Decimal("250.50"), reference="teste")
    assert novo == Decimal("750.50")

    restante = debit_balance(db, USER_ID, Decimal("100.50"))
    assert restante == Decimal("650.00")

    with pytest.raises(BusinessError) as erro:
        debit_balance(db, USER_ID, Decimal("99999"))
    assert erro.value.code == "insufficient_funds"


def test_fluxo_completo_sem_funcoes_sql(fake_sb, settings):
    from tests.fakes import FakeNOWPayments

    fake_sb.rpc_enabled = False
    db = fake_sb.client()
    seed_profile(fake_sb)

    falso = FakeNOWPayments()
    linha = create_payment(db, falso.client(), settings, user_id=USER_ID, email=EMAIL, request=payment_request(1000))
    handle_ipn(db, {"order_id": linha["order_id"], "payment_status": "finished", "actually_paid": 12.5})
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 1000

    payout = create_payout(db, settings, user_id=USER_ID, email=EMAIL, request=payout_request(600))
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 400

    decide_payout(db, payout_id=payout["id"], decision="reject", note="", admin_id="admin-1")
    assert fake_sb.tables["profiles"][0]["balance_mt"] == 1000


# --------------------------------------------------------------------------- #
# Resiliencia: a app continua a responder quando a Supabase esta inacessivel
# --------------------------------------------------------------------------- #
def test_taxa_usa_o_env_quando_a_rede_falha(settings):
    import httpx

    from app.db import SupabaseClient

    def rebentar(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede", request=request)

    db = SupabaseClient(
        "https://fake.supabase.co", "sb_secret_teste", transport=httpx.MockTransport(rebentar)
    )
    assert get_rate(db, settings) == Decimal("80")
