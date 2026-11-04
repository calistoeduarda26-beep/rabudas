"""Levantamentos para M-Pesa / e-Mola."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.config import Settings, get_settings
from app.db import SupabaseClient
from app.deps import current_user, db_dep, profile_of, translate_business_error
from app.schemas import PayoutCreateRequest, PayoutOut, as_money
from app.security import mask_phone
from app.services import BusinessError, create_payout, list_payouts

log = logging.getLogger("rabudas.payouts")

router = APIRouter(prefix="/api/payouts", tags=["payouts"])


def payout_out(row: dict) -> PayoutOut:
    profiles = row.get("profiles")
    email = row.get("email") or ""
    if isinstance(profiles, dict):
        email = profiles.get("email") or email
    elif isinstance(profiles, list) and profiles:
        email = profiles[0].get("email") or email
    return PayoutOut(
        id=row.get("id", ""),
        user_id=row.get("user_id", ""),
        email=email,
        status=row.get("status", "pending"),
        amount_mt=as_money(row.get("amount_mt")),
        method=row.get("method", "mpesa"),
        phone_masked=mask_phone(row.get("phone", "")),
        holder_name=row.get("holder_name", "") or "",
        admin_note=row.get("admin_note", "") or "",
        created_at=str(row.get("created_at")) if row.get("created_at") else None,
        decided_at=str(row.get("decided_at")) if row.get("decided_at") else None,
    )


@router.post("", response_model=PayoutOut, status_code=201)
def request_payout(
    body: PayoutCreateRequest,
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> PayoutOut:
    try:
        row = create_payout(
            db, settings, user_id=user["user_id"], email=user["email"], request=body
        )
    except BusinessError as exc:
        raise translate_business_error(exc) from exc
    log.info(
        "Pedido de levantamento %s MT por %s (%s)",
        row.get("amount_mt"),
        user["email"],
        mask_phone(body.phone),
    )
    return payout_out(row)


@router.get("", response_model=list[PayoutOut])
def my_payouts(
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
) -> list[PayoutOut]:
    rows = list_payouts(db, user_id=user["user_id"], limit=limit)
    return [payout_out(row) for row in rows]
