"""Webhook IPN da NOWPayments.

A assinatura `x-nowpayments-sig` (HMAC-SHA512) e verificada com
`NOWPAYMENTS_IPN_SECRET` antes de qualquer escrita. Sem assinatura valida nao
ha efeito nenhum na base de dados.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.db import SupabaseClient, SupabaseError
from app.deps import db_dep
from app.nowpayments import verify_ipn_signature
from app.services import BusinessError, handle_ipn

log = logging.getLogger("rabudas.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/nowpayments")
async def nowpayments_ipn(
    request: Request,
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    if not settings.ipn_enabled:
        # Nao ha segredo configurado: recusamos tudo em vez de aceitar cegamente.
        log.warning("IPN recebido mas NOWPAYMENTS_IPN_SECRET nao esta configurado.")
        raise HTTPException(status_code=503, detail="IPN nao configurado no servidor.")

    raw = await request.body()
    signature = request.headers.get("x-nowpayments-sig", "")
    if not verify_ipn_signature(raw, signature, settings.NOWPAYMENTS_IPN_SECRET.get_secret_value()):
        log.warning("IPN com assinatura invalida de %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="Assinatura invalida.")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Corpo JSON invalido.") from exc

    order_id = payload.get("order_id", "?")
    np_status = payload.get("payment_status", "?")
    try:
        payment = handle_ipn(db, payload)
    except BusinessError as exc:
        log.warning("IPN %s ignorado: %s", order_id, exc.message)
        # 200 para a NOWPayments nao repetir notificacoes inuteis.
        return {"ok": False, "reason": exc.code}
    except SupabaseError as exc:
        log.error("IPN %s falhou na base de dados (%s)", order_id, exc.status_code)
        raise HTTPException(status_code=503, detail="Base de dados indisponivel.") from exc

    log.info("IPN %s -> %s (estado %s)", order_id, payment.get("status"), np_status)
    return {"ok": True, "status": payment.get("status")}
