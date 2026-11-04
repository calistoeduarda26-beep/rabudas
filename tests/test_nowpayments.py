"""NOWPayments: assinatura IPN, mapeamento de estados e cliente HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from app.nowpayments import (
    NOWPaymentsClient,
    NOWPaymentsError,
    map_status,
    sign_ipn_payload,
    verify_ipn_signature,
)
from tests.fakes import FakeNOWPayments

SEGRED0 = "segredo_ipn_teste"


def payload_exemplo() -> dict:
    return {
        "payment_id": "9000001",
        "payment_status": "finished",
        "pay_address": "TQn9Y2khDD95J42FQtQTdwBMRk1XfK6Z9p",
        "price_amount": 12.5,
        "price_currency": "usd",
        "actually_paid": 12.5,
        "pay_currency": "usdttrc20",
        "order_id": "RBD-ABC123",
        "outcome_amount": 12.5,
        "outcome_currency": "usdttrc20",
    }


def corpo(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_assinatura_valida_e_aceite():
    payload = payload_exemplo()
    assinatura = sign_ipn_payload(payload, SEGRED0)
    assert verify_ipn_signature(corpo(payload), assinatura, SEGRED0) is True


def test_assinatura_aceite_mesmo_com_json_reordenado():
    payload = payload_exemplo()
    assinatura = sign_ipn_payload(payload, SEGRED0)
    reordenado = json.dumps(dict(reversed(list(payload.items()))), indent=2).encode("utf-8")
    assert verify_ipn_signature(reordenado, assinatura, SEGRED0) is True


def test_corpo_alterado_e_recusado():
    payload = payload_exemplo()
    assinatura = sign_ipn_payload(payload, SEGRED0)
    payload["payment_status"] = "finished"
    payload["actually_paid"] = 999.0
    assert verify_ipn_signature(corpo(payload), assinatura, SEGRED0) is False


def test_segredo_errado_e_recusado():
    payload = payload_exemplo()
    assinatura = sign_ipn_payload(payload, SEGRED0)
    assert verify_ipn_signature(corpo(payload), assinatura, "outro_segredo") is False


def test_sem_assinatura_ou_sem_segredo_e_recusado():
    payload = payload_exemplo()
    assert verify_ipn_signature(corpo(payload), "", SEGRED0) is False
    assert verify_ipn_signature(corpo(payload), sign_ipn_payload(payload, SEGRED0), "") is False


def test_corpo_invalido_e_recusado():
    assert verify_ipn_signature(b"nao-e-json", "abc", SEGRED0) is False


@pytest.mark.parametrize(
    "estado_nowpayments,esperado",
    [
        ("waiting", "pending"),
        ("confirming", "processing"),
        ("confirmed", "processing"),
        ("sending", "processing"),
        ("partially_paid", "partial"),
        ("finished", "paid"),
        ("failed", "failed"),
        ("refunded", "refunded"),
        ("expired", "expired"),
        ("desconhecido", "unknown"),
        ("", "unknown"),
    ],
)
def test_mapeamento_de_estados(estado_nowpayments, esperado):
    assert map_status(estado_nowpayments) == esperado


# --------------------------------------------------------------------------- #
# Cliente HTTP
# --------------------------------------------------------------------------- #
def test_cliente_envia_chave_no_header_e_cria_pagamento():
    fake = FakeNOWPayments()
    cliente = fake.client()
    resposta = cliente.create_payment(
        {
            "price_amount": 12.5,
            "price_currency": "usd",
            "pay_currency": "usdttrc20",
            "order_id": "RBD-1",
            "ipn_callback_url": "http://localhost:8000/api/webhooks/nowpayments",
        }
    )
    assert resposta["payment_id"] == "9000001"
    assert fake.created[0]["price_amount"] == 12.5
    cliente.close()


def test_cliente_levanta_erro_em_falha_da_api():
    fake = FakeNOWPayments(fail_create=True)
    cliente = fake.client()
    with pytest.raises(NOWPaymentsError) as erro:
        cliente.create_payment({"price_amount": 1})
    assert erro.value.status_code == 503
    cliente.close()


def test_cliente_sem_chave_e_recusado():
    with pytest.raises(ValueError):
        NOWPaymentsClient("", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def test_get_payment_devolve_estado():
    fake = FakeNOWPayments(status="finished")
    fake.created.append({"price_amount": 12.5, "order_id": "RBD-1"})
    cliente = fake.client()
    dados = cliente.get_payment("9000001")
    assert dados["payment_status"] == "finished"
    assert fake.fetched == ["9000001"]
    cliente.close()
