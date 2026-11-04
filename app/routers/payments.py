"""Depositos em criptomoeda via NOWPayments."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.config import Settings, get_settings
from app.db import SupabaseClient
from app.deps import current_user, db_dep, request_base_url, translate_business_error
from app.nowpayments import NOWPaymentsClient, NOWPaymentsError, get_nowpayments
from app.schemas import PaymentCreateRequest, PaymentOut, as_money
from app.services import BusinessError, create_payment, get_rate, list_payments, sync_payment
from app.security import mt_to_usdt

log = logging.getLogger("rabudas.payments")

router = APIRouter(prefix="/api/payments", tags=["payments"])


def payment_out(row: dict, email: str = "") -> PaymentOut:
    return PaymentOut(
        id=row.get("id", ""),
        order_id=row.get("order_id", ""),
        status=row.get("status", "pending"),
        amount_mt=as_money(row.get("amount_mt")),
        amount_usdt=as_money(row.get("amount_usdt")),
        pay_currency=row.get("pay_currency", "usdttrc20"),
        pay_address=row.get("pay_address"),
        payin_extra_id=row.get("payin_extra_id"),
        payment_url=row.get("payment_url"),
        np_payment_id=str(row.get("np_payment_id") or "") or None,
        np_status=row.get("np_status"),
        credited_mt=as_money(row.get("credited_mt")),
        note=row.get("note", "") or "",
        created_at=str(row.get("created_at")) if row.get("created_at") else None,
        updated_at=str(row.get("updated_at")) if row.get("updated_at") else None,
    )


@router.get("/quote")
def quote(
    amount_mt: float = Query(1000, gt=0, le=1_000_000),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Indicacao publica: quanto custa um deposito em USDT para a taxa actual."""
    from decimal import Decimal

    rate = get_rate(db, settings)
    usdt = mt_to_usdt(Decimal(str(amount_mt)), rate)
    return {
        "amount_mt": f"{amount_mt:.2f}",
        "amount_usdt": str(usdt),
        "rate_mt_per_usdt": float(rate),
        "min_amount_mt": 100,
    }


@router.post("", response_model=PaymentOut, status_code=201)
def create(
    body: PaymentCreateRequest,
    request: Request,
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
    np_client: NOWPaymentsClient = Depends(get_nowpayments),
) -> PaymentOut:
    try:
        row = create_payment(
            db,
            np_client,
            settings,
            user_id=user["user_id"],
            email=user["email"],
            request=body,
            base_url=request_base_url(request, settings),
        )
    except BusinessError as exc:
        raise translate_business_error(exc) from exc
    except NOWPaymentsError as exc:
        raise HTTPException(status_code=502, detail="A NOWPayments nao respondeu.") from exc
    return payment_out(row, user["email"])


@router.get("", response_model=list[PaymentOut])
def my_payments(
    limit: int = Query(50, ge=1, le=200),
    status_filter: Optional[str] = Query(None, alias="status"),
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
) -> list[PaymentOut]:
    rows = list_payments(db, user_id=user["user_id"], status=status_filter or "", limit=limit)
    return [payment_out(row) for row in rows]


@router.get("/{payment_id}", response_model=PaymentOut)
def payment_detail(
    payment_id: str,
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
    np_client: NOWPaymentsClient = Depends(get_nowpayments),
) -> PaymentOut:
    from app.db import SupabaseError

    try:
        row = db.select_one("payments", id=payment_id)
    except SupabaseError as exc:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel.") from exc
    if not row:
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado.")
    if row.get("user_id") != user["user_id"] and (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="Sem acesso a este pagamento.")

    try:
        row = sync_payment(db, np_client, payment_id)
    except BusinessError as exc:
        raise translate_business_error(exc) from exc
    except NOWPaymentsError as exc:
        log.warning("Sincronizacao NOWPayments falhou: %s", exc.message)
    return payment_out(row)
