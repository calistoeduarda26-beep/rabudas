"""Modelos Pydantic (validacao de entrada e saida da API)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security import normalize_mz_phone, normalize_mt

PayoutMethod = Literal["mpesa", "emola"]


def new_uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class SignUpRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=120)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Email invalido.")
        return v


class SignInRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


class SessionResponse(BaseModel):
    user_id: str
    email: str
    full_name: str = ""
    role: str = "user"
    balance_mt: str = "0.00"


# --------------------------------------------------------------------------- #
# Pagamentos (depositos em cripto)
# --------------------------------------------------------------------------- #
class PaymentCreateRequest(BaseModel):
    amount_mt: float = Field(gt=0, description="Valor em Meticais")
    pay_currency: str = Field(default="usdttrc20", max_length=20)
    note: str = Field(default="", max_length=200)

    @field_validator("amount_mt")
    @classmethod
    def _amount(cls, v: float) -> float:
        amount = normalize_mt(v)
        if amount < 100:
            raise ValueError("O deposito minimo e de 100 MT.")
        if amount > 1_000_000:
            raise ValueError("O deposito maximo por transacao e de 1.000.000 MT.")
        return float(amount)


class PaymentOut(BaseModel):
    id: str
    order_id: str
    status: str
    amount_mt: str
    amount_usdt: str
    pay_currency: str
    pay_address: Optional[str] = None
    payin_extra_id: Optional[str] = None
    payment_url: Optional[str] = None
    np_payment_id: Optional[str] = None
    np_status: Optional[str] = None
    credited_mt: str = "0.00"
    note: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Levantamentos
# --------------------------------------------------------------------------- #
class PayoutCreateRequest(BaseModel):
    amount_mt: float = Field(gt=0)
    method: PayoutMethod = "mpesa"
    phone: str = Field(min_length=8, max_length=20)
    holder_name: str = Field(default="", max_length=120)

    @field_validator("amount_mt")
    @classmethod
    def _amount(cls, v: float) -> float:
        amount = normalize_mt(v)
        if amount < 200:
            raise ValueError("O levantamento minimo e de 200 MT.")
        return float(amount)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_mz_phone(v)


class PayoutOut(BaseModel):
    id: str
    user_id: str
    email: str = ""
    status: str
    amount_mt: str
    method: str
    phone_masked: str
    holder_name: str = ""
    admin_note: str = ""
    created_at: Optional[str] = None
    decided_at: Optional[str] = None


class PayoutDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=300)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class RateUpdateRequest(BaseModel):
    rate_mt_per_usdt: float = Field(gt=0, le=100000)


class AdminUserRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    email: str = ""
    full_name: str = ""
    role: str = "user"
    balance_mt: str = "0.00"
    created_at: Optional[str] = None


class LoginLogOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Any = None
    email: str = ""
    success: bool = False
    reason: str = ""
    ip: str = ""
    user_agent: str = ""
    created_at: Optional[str] = None


class StatsOut(BaseModel):
    users: int = 0
    payments_pending: int = 0
    payments_paid: int = 0
    total_credited_mt: float = 0.0
    payouts_pending: int = 0
    payouts_approved: int = 0
    total_paid_out_mt: float = 0.0
    rate_mt_per_usdt: float = 80.0


def as_money(value: Any) -> str:
    """Formata um valor numerico como string com 2 casas (sem perder precisao)."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def utcnow() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
