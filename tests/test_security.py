"""Tokens de sessao, rate limiting e conversoes de dinheiro."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from app.security import (
    RateLimiter,
    mask_email,
    mask_phone,
    mt_to_usdt,
    normalize_mz_phone,
    normalize_mt,
    sign_token,
    usdt_to_mt,
    verify_token,
)

SEGRED0 = "s" * 64


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def test_token_valido_devolve_payload():
    token = sign_token({"uid": "u1", "at": "tok"}, SEGRED0, ttl_seconds=60)
    payload = verify_token(token, SEGRED0)
    assert payload is not None
    assert payload["uid"] == "u1"
    assert payload["at"] == "tok"
    assert payload["exp"] > int(time.time())


def test_token_expirado_e_recusado():
    token = sign_token({"uid": "u1"}, SEGRED0, ttl_seconds=-10)
    assert verify_token(token, SEGRED0) is None


def test_token_adulterado_e_recusado():
    token = sign_token({"uid": "u1"}, SEGRED0, ttl_seconds=60)
    corpo, _, assinatura = token.partition(".")
    assert verify_token(corpo[:-1] + ("A" if corpo[-1] != "A" else "B") + "." + assinatura, SEGRED0) is None


def test_token_com_outro_segredo_e_recusado():
    token = sign_token({"uid": "u1"}, SEGRED0, ttl_seconds=60)
    assert verify_token(token, "x" * 64) is None
    assert verify_token("", SEGRED0) is None
    assert verify_token("sem-ponto", SEGRED0) is None


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def test_rate_limiter_bloqueia_depois_do_limite():
    limiter = RateLimiter(max_attempts=3, window_seconds=60)
    chave = "login:1.2.3.4:a@b.c"
    for _ in range(3):
        bloqueado, _ = limiter.blocked(chave)
        assert bloqueado is False
        limiter.register(chave)
    bloqueado, espera = limiter.blocked(chave)
    assert bloqueado is True
    assert espera > 0


def test_rate_limiter_limpa_a_chave():
    limiter = RateLimiter(max_attempts=1, window_seconds=60)
    limiter.register("k")
    assert limiter.blocked("k")[0] is True
    limiter.clear("k")
    assert limiter.blocked("k")[0] is False


# --------------------------------------------------------------------------- #
# Dinheiro
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mt,taxa,usdt",
    [
        (1000, 80, "12.50"),
        (80, 80, "1.00"),
        (100, 80, "1.25"),
        (1234.56, 80, "15.43"),
    ],
)
def test_mt_para_usdt(mt, taxa, usdt):
    assert str(mt_to_usdt(mt, taxa)) == usdt


def test_usdt_para_mt():
    assert usdt_to_mt(12.5, 80) == Decimal("1000.00")
    assert usdt_to_mt(1, 80) == Decimal("80.00")


def test_normalize_mt_arredonda():
    assert normalize_mt("1000.005") == Decimal("1000.01")
    assert normalize_mt(0) == Decimal("0.00")


def test_ida_e_volta_mt_usdt():
    valor = Decimal("5000.00")
    assert usdt_to_mt(mt_to_usdt(valor, 80), 80) == valor


# --------------------------------------------------------------------------- #
# Telefones e mascaras
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("841234567", "+258841234567"),
        ("+258841234567", "+258841234567"),
        ("258 84 123 4567", "+258841234567"),
        ("861234567", "+258861234567"),
    ],
)
def test_normaliza_telefone_mocambicano(entrada, esperado):
    assert normalize_mz_phone(entrada) == esperado


@pytest.mark.parametrize("entrada", ["12345", "8412345", "+258991234567", "abc", ""])
def test_telefone_invalido_e_recusado(entrada):
    with pytest.raises(ValueError):
        normalize_mz_phone(entrada)


def test_mascaras():
    assert mask_phone("+258841234567") == "25884***67"
    assert mask_email("joao.silva@exemplo.mz") == "jo***@exemplo.mz"
    assert mask_email("sem-arroba") == "***"
    assert mask_phone("12") == "***"
