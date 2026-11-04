"""Tarefas de administracao (exige `profiles.role = 'admin'`)."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.db import SupabaseClient, SupabaseError
from app.deps import current_admin, db_dep
from app.nowpayments import NOWPaymentsClient, NOWPaymentsError, get_nowpayments
from app.schemas import (
    AdminUserRow,
    LoginLogOut,
    MinDepositUpdateRequest,
    PayoutDecisionRequest,
    RateUpdateRequest,
    StatsOut,
)
from app.services import BusinessError, decide_payout, list_login_logs, list_payments, list_payouts, stats
from app.routers.payments import payment_out
from app.routers.payouts import payout_out

log = logging.getLogger("rabudas.admin")

#: `current_admin` e injectado em cada rota (da-nos o admin autenticado);
#: nao se repete ao nivel do router para nao validar a sessao duas vezes.
router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats", response_model=StatsOut)
def admin_stats(
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> StatsOut:
    try:
        return StatsOut(**stats(db, settings))
    except SupabaseError as exc:
        raise HTTPException(status_code=503, detail=f"Base de dados indisponivel ({exc.status_code}).") from exc


@router.get("/payouts")
def admin_payouts(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> list:
    rows = list_payouts(db, status=status or "", limit=limit)
    return [payout_out(row).model_dump() for row in rows]


@router.post("/payouts/{payout_id}/decision")
def admin_decide(
    payout_id: str,
    body: PayoutDecisionRequest,
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> dict:
    try:
        row = decide_payout(
            db,
            payout_id=payout_id,
            decision=body.decision,
            note=body.note,
            admin_id=admin["user_id"],
        )
    except BusinessError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except SupabaseError as exc:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel.") from exc
    log.info("Admin %s decidiu %s -> %s", admin["email"], payout_id, body.decision)
    return payout_out(row).model_dump()


@router.get("/payments")
def admin_payments(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> list:
    rows = list_payments(db, status=status or "", limit=limit)
    return [payment_out(row).model_dump() for row in rows]


@router.get("/login-logs", response_model=list[LoginLogOut])
def admin_login_logs(
    email: str = Query("", max_length=254),
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> list[LoginLogOut]:
    rows = list_login_logs(db, email=email, limit=limit)
    out: list[LoginLogOut] = []
    for row in rows:
        out.append(
            LoginLogOut(
                id=str(row.get("id") or ""),
                email=row.get("email", "") or "",
                success=bool(row.get("success")),
                reason=row.get("reason", "") or "",
                ip=row.get("ip", "") or "",
                user_agent=row.get("user_agent", "") or "",
                created_at=str(row.get("created_at")) if row.get("created_at") else None,
            )
        )
    return out


@router.get("/users", response_model=list[AdminUserRow])
def admin_users(
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> list[AdminUserRow]:
    rows = db.table("profiles").select("*").order("created_at", desc=False).limit(limit).execute() or []
    out: list[AdminUserRow] = []
    for row in rows:
        out.append(
            AdminUserRow(
                id=row.get("id", ""),
                email=row.get("email", "") or "",
                full_name=row.get("full_name", "") or "",
                role=row.get("role", "user") or "user",
                balance_mt=f"{float(row.get('balance_mt') or 0):.2f}",
                created_at=str(row.get("created_at")) if row.get("created_at") else None,
            )
        )
    return out


@router.post("/users/{user_id}/role")
def admin_set_role(
    user_id: str,
    role: str = Query(..., pattern="^(user|admin)$"),
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> dict:
    updated = db.update_where("profiles", {"role": role}, id=user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Utilizador nao encontrado.")
    log.info("Admin %s mudou papel de %s para %s", admin["email"], user_id, role)
    return {"id": user_id, "role": role}


@router.get("/rate")
def admin_get_rate(
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    from app.services import get_rate

    return {
        "rate_mt_per_usdt": float(get_rate(db, settings)),
        "env_default": float(settings.RATE_MT_PER_USDT),
    }


@router.put("/rate", response_model=dict)
def admin_set_rate(
    body: RateUpdateRequest,
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
) -> dict:
    try:
        db.upsert("settings", {"key": "rate_mt_per_usdt", "value": str(body.rate_mt_per_usdt)})
    except SupabaseError as exc:
        raise HTTPException(
            status_code=503,
            detail="Nao foi possivel gravar a taxa. Corre supabase/schema.sql primeiro.",
        ) from exc
    log.info("Admin %s definiu taxa %s MT/USDT", admin["email"], body.rate_mt_per_usdt)
    return {"rate_mt_per_usdt": body.rate_mt_per_usdt}


@router.get("/min-deposit")
def admin_get_min_deposit(
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    from app.services import get_min_deposit_mt, get_min_deposit_usd, get_rate

    min_usd = get_min_deposit_usd(db, settings)
    return {
        "min_deposit_usd": float(min_usd),
        "min_deposit_mt": float(get_min_deposit_mt(db, settings)),
        "rate_mt_per_usdt": float(get_rate(db, settings)),
        "env_default": float(settings.MIN_DEPOSIT_USD),
    }


@router.put("/min-deposit", response_model=dict)
def admin_set_min_deposit(
    body: MinDepositUpdateRequest,
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    from app.services import get_min_deposit_mt, get_rate

    try:
        db.upsert("settings", {"key": "min_deposit_usd", "value": str(body.min_deposit_usd)})
    except SupabaseError as exc:
        raise HTTPException(
            status_code=503,
            detail="Nao foi possivel gravar o minimo. Corre supabase/schema.sql primeiro.",
        ) from exc
    log.info("Admin %s definiu deposito minimo %s USD", admin["email"], body.min_deposit_usd)
    return {
        "min_deposit_usd": body.min_deposit_usd,
        "min_deposit_mt": float(get_min_deposit_mt(db, settings)),
    }


@router.get("/health-check")
def admin_health_check(
    admin: dict = Depends(current_admin),
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
    np_client: NOWPaymentsClient = Depends(get_nowpayments),
) -> dict:
    """Diagnostico para o painel: diz o que esta ligado e o que falta configurar."""
    report = {
        "supabase": "ok",
        "supabase_detail": "",
        "nowpayments": "ok",
        "nowpayments_detail": "",
        "ipn": "ok" if settings.ipn_enabled else "por_configurar",
        "ipn_detail": (
            "NOWPAYMENTS_IPN_SECRET definido"
            if settings.ipn_enabled
            else "Define NOWPAYMENTS_IPN_SECRET no .env (NOWPayments -> Store settings -> IPN)"
        ),
        "ipn_callback_url": f"{settings.BASE_URL.rstrip('/')}/api/webhooks/nowpayments",
    }
    try:
        db.table("profiles").select("id").limit(1).execute()
    except SupabaseError as exc:
        report["supabase"] = "erro"
        report["supabase_detail"] = exc.message[:200]
    except Exception as exc:  # pragma: no cover - rede
        report["supabase"] = "erro"
        report["supabase_detail"] = str(exc)[:200]

    try:
        status_payload = np_client.status()
        report["nowpayments_detail"] = str(status_payload.get("message", ""))[:100]
    except NOWPaymentsError as exc:
        report["nowpayments"] = "erro"
        report["nowpayments_detail"] = exc.message[:200]
    except Exception as exc:  # pragma: no cover - rede
        report["nowpayments"] = "erro"
        report["nowpayments_detail"] = str(exc)[:200]
    return report
