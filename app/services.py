"""Regras de negocio: depositos, creditos, levantamentos e taxa de cambio.

Todas as funcoes recebem o cliente Supabase como argumento, o que permite
testa-las com um transporte HTTP falso (sem tocar na base de dados real).

Estrategia de consistencia:
1. Primeiro tenta funcoes SQL atomicas (`rpc`) definidas em `supabase/schema.sql`.
2. Se a base ainda nao tiver essas funcoes, usa actualizacao condicional
   (optimistic locking) sobre `profiles.balance_mt`.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Any, Optional

from app.config import Settings
from app.db import SupabaseClient, SupabaseError
from app.nowpayments import NOWPaymentsClient, map_status
from app.schemas import PaymentCreateRequest, PayoutCreateRequest
from app.security import mt_to_usdt, normalize_mt, usdt_to_mt


def utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

log = logging.getLogger("rabudas.services")

PAYOUT_STATUS_PENDING = "pending"
PAYOUT_STATUS_APPROVED = "approved"
PAYOUT_STATUS_REJECTED = "rejected"


class BusinessError(Exception):
    """Erro de negocio com mensagem segura para mostrar ao utilizador."""

    def __init__(self, message: str, code: str = "business_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _rpc_missing(exc: SupabaseError) -> bool:
    text = f"{exc.message} {exc.payload}".lower()
    return (
        "could not find the function" in text
        or "pgrst202" in text
        or "function public." in text
        or "42883" in text
    )


# --------------------------------------------------------------------------- #
# Perfis e taxa
# --------------------------------------------------------------------------- #
def ensure_profile(db: SupabaseClient, user_id: str, email: str, full_name: str = "") -> dict:
    profile = db.select_one("profiles", id=user_id)
    if profile:
        if full_name and not profile.get("full_name"):
            db.update_where("profiles", {"full_name": full_name}, id=user_id)
            profile["full_name"] = full_name
        return profile
    payload = {
        "id": user_id,
        "email": email,
        "full_name": full_name or "",
        "role": "user",
        "balance_mt": 0,
    }
    try:
        rows = db.insert("profiles", payload)
        return rows[0] if rows else payload
    except SupabaseError as exc:  # concorrencia: outro pedido ja criou a linha
        if exc.status_code == 409:
            return db.select_one("profiles", id=user_id) or payload
        raise


def get_profile(db: SupabaseClient, user_id: str) -> dict:
    profile = db.select_one("profiles", id=user_id)
    if not profile:
        raise BusinessError("Perfil nao encontrado. Faz logout e volta a entrar.", "no_profile")
    return profile


def get_rate(db: SupabaseClient, settings: Settings) -> Decimal:
    """Taxa MT/USDT: valor da tabela `settings` (admin) com fallback para o `.env`.

    Se a base de dados estiver em baixo ou inacessivel, usa a taxa do `.env`
    em vez de falhar o pedido.
    """
    try:
        row = db.select_one("settings", key="rate_mt_per_usdt")
        if row and row.get("value") not in (None, ""):
            return Decimal(str(row["value"]))
    except SupabaseError as exc:
        log.warning(
            "Nao foi possivel ler settings (%s); a usar RATE_MT_PER_USDT do .env", exc.status_code
        )
    except Exception as exc:  # rede em baixo, DNS, timeout
        log.warning("Sem acesso a Supabase (%s); a usar RATE_MT_PER_USDT do .env", exc)
    return Decimal(str(settings.RATE_MT_PER_USDT))


def balance_of(profile: dict) -> Decimal:
    return normalize_mt(profile.get("balance_mt") or 0)


# --------------------------------------------------------------------------- #
# Movimentos de saldo
# --------------------------------------------------------------------------- #
def credit_balance(
    db: SupabaseClient, user_id: str, amount_mt: Decimal, *, reference: str = ""
) -> Decimal:
    amount = normalize_mt(amount_mt)
    if amount <= 0:
        raise BusinessError("Valor a creditar invalido.", "invalid_amount")
    try:
        result = db.rpc(
            "credit_balance",
            {"p_user_id": user_id, "p_amount": str(amount), "p_reference": reference},
        )
        if isinstance(result, dict) and result.get("ok"):
            return normalize_mt(result.get("balance", 0))
    except SupabaseError as exc:
        if not _rpc_missing(exc):
            raise

    # Fallback sem funcao SQL: leitura + actualizacao condicional.
    for _ in range(3):
        profile = db.select_one("profiles", id=user_id)
        if not profile:
            raise BusinessError("Perfil nao encontrado.", "no_profile")
        current = balance_of(profile)
        new_balance = current + amount
        updated = db.update_where(
            "profiles", {"balance_mt": float(new_balance)}, id=user_id, balance_mt=str(current)
        )
        if updated:
            return new_balance
        log.warning("Concorrencia ao creditar saldo de %s; a tentar novamente", user_id)
    raise BusinessError("Nao foi possivel actualizar o saldo. Tenta outra vez.", "race")


def debit_balance(db: SupabaseClient, user_id: str, amount_mt: Decimal) -> Decimal:
    amount = normalize_mt(amount_mt)
    if amount <= 0:
        raise BusinessError("Valor invalido.", "invalid_amount")
    try:
        result = db.rpc("debit_balance", {"p_user_id": user_id, "p_amount": str(amount)})
        if isinstance(result, dict):
            if result.get("ok"):
                return normalize_mt(result.get("balance", 0))
            if result.get("reason") == "insufficient_funds":
                raise BusinessError("Saldo insuficiente.", "insufficient_funds")
    except SupabaseError as exc:
        if not _rpc_missing(exc):
            raise

    for _ in range(3):
        profile = db.select_one("profiles", id=user_id)
        if not profile:
            raise BusinessError("Perfil nao encontrado.", "no_profile")
        current = balance_of(profile)
        if current < amount:
            raise BusinessError(
                f"Saldo insuficiente. Tens {current} MT disponiveis.", "insufficient_funds"
            )
        new_balance = current - amount
        updated = db.update_where(
            "profiles", {"balance_mt": float(new_balance)}, id=user_id, balance_mt=str(current)
        )
        if updated:
            return new_balance
        log.warning("Concorrencia ao debitar saldo de %s; a tentar novamente", user_id)
    raise BusinessError("Nao foi possivel actualizar o saldo. Tenta outra vez.", "race")


# --------------------------------------------------------------------------- #
# Depositos (pagamentos NOWPayments)
# --------------------------------------------------------------------------- #
def new_order_id() -> str:
    return f"RBD-{secrets.token_hex(4).upper()}"


def create_payment(
    db: SupabaseClient,
    np_client: NOWPaymentsClient,
    settings: Settings,
    *,
    user_id: str,
    email: str,
    request: PaymentCreateRequest,
) -> dict:
    rate = get_rate(db, settings)
    amount_mt = normalize_mt(request.amount_mt)
    amount_usdt = mt_to_usdt(amount_mt, rate)
    if amount_usdt < Decimal("0.01"):
        raise BusinessError("Valor demasiado baixo para pagamento em cripto.", "too_small")

    order_id = new_order_id()
    row = {
        "id": secrets.token_hex(8) + secrets.token_hex(8),
        "user_id": user_id,
        "order_id": order_id,
        "amount_mt": str(amount_mt),
        "amount_usdt": str(amount_usdt),
        "pay_currency": request.pay_currency,
        "status": "pending",
        "np_status": "created",
        "credited_mt": "0.00",
        "note": request.note or "",
        "rate_mt_per_usdt": str(rate),
    }
    db.insert("payments", row)

    base = settings.BASE_URL.rstrip("/")
    payload = {
        "price_amount": float(amount_usdt),
        "price_currency": "usd",
        "pay_currency": request.pay_currency,
        "order_id": order_id,
        "order_description": f"Deposito {amount_mt} MT",
        "ipn_callback_url": f"{base}/api/webhooks/nowpayments",
        "success_url": f"{base}/dashboard.html?payment=success&order_id={order_id}",
        "cancel_url": f"{base}/dashboard.html?payment=cancelled&order_id={order_id}",
    }
    try:
        created = np_client.create_payment(payload)
    except Exception as exc:
        db.update_where("payments", {"status": "failed", "np_status": "api_error"}, id=row["id"])
        log.error("NOWPayments falhou ao criar pagamento: %s", exc)
        raise BusinessError(
            "Nao foi possivel criar o pagamento na NOWPayments. Tenta mais tarde.",
            "np_error",
        ) from exc

    update = {
        "np_payment_id": str(created.get("payment_id") or ""),
        "np_status": created.get("payment_status") or "waiting",
        "pay_address": created.get("pay_address"),
        "payin_extra_id": created.get("payin_extra_id"),
        "payment_url": created.get("payment_url"),
        "invoice_id": created.get("invoice_id"),
    }
    db.update_where("payments", update, id=row["id"])
    row.update({k: v for k, v in update.items() if v is not None})
    row["user_email"] = email
    return row


def _apply_np_state(db: SupabaseClient, payment: dict, np_payload: dict) -> dict:
    """Actualiza um pagamento com o estado vindo da NOWPayments e credita se pago."""
    np_status = (np_payload.get("payment_status") or "").lower()
    status = map_status(np_status)
    payment_id = payment["id"]

    db.update_where(
        "payments",
        {
            "status": status,
            "np_status": np_status or payment.get("np_status"),
            "actually_paid": str(np_payload.get("actually_paid") or "0"),
            "outcome_amount": str(np_payload.get("outcome_amount") or "0"),
            "outcome_currency": np_payload.get("outcome_currency"),
            "pay_address": np_payload.get("pay_address") or payment.get("pay_address"),
            "updated_at": utcnow_iso(),
        },
        id=payment_id,
    )
    payment["status"] = status
    payment["np_status"] = np_status

    if status == "paid" and normalize_mt(payment.get("credited_mt") or 0) <= 0:
        rate = Decimal(str(payment.get("rate_mt_per_usdt") or 0)) or None
        credited = (
            usdt_to_mt(np_payload.get("actually_paid"), rate)
            if rate and np_payload.get("actually_paid")
            else normalize_mt(payment.get("amount_mt") or 0)
        )
        new_balance = credit_balance(
            db, payment["user_id"], credited, reference=payment.get("order_id", "")
        )
        db.update_where("payments", {"credited_mt": str(credited)}, id=payment_id)
        payment["credited_mt"] = str(credited)
        payment["balance_mt"] = str(new_balance)
        log.info(
            "Pagamento %s creditado: %s MT (novo saldo %s MT)",
            payment.get("order_id"),
            credited,
            new_balance,
        )
    return payment


def sync_payment(db: SupabaseClient, np_client: NOWPaymentsClient, payment_id: str) -> dict:
    payment = db.select_one("payments", id=payment_id)
    if not payment:
        raise BusinessError("Pagamento nao encontrado.", "not_found")
    if payment.get("status") == "paid":
        return payment
    if not payment.get("np_payment_id"):
        return payment
    payload = np_client.get_payment(payment["np_payment_id"])
    return _apply_np_state(db, payment, payload)


def handle_ipn(db: SupabaseClient, payload: dict) -> dict:
    """Processa uma notificacao IPN ja verificada (assinatura validada antes)."""
    order_id = payload.get("order_id")
    if not order_id:
        raise BusinessError("IPN sem order_id.", "bad_ipn")
    payment = db.select_one("payments", order_id=order_id)
    if not payment:
        raise BusinessError(f"IPN para order_id desconhecido: {order_id}", "unknown_order")
    return _apply_np_state(db, payment, payload)


# --------------------------------------------------------------------------- #
# Levantamentos
# --------------------------------------------------------------------------- #
def create_payout(
    db: SupabaseClient,
    settings: Settings,
    *,
    user_id: str,
    email: str,
    request: PayoutCreateRequest,
) -> dict:
    amount = normalize_mt(request.amount_mt)

    try:
        result = db.rpc(
            "request_payout",
            {
                "p_user_id": user_id,
                "p_amount": str(amount),
                "p_method": request.method,
                "p_phone": request.phone,
                "p_holder": request.holder_name or "",
            },
        )
        if isinstance(result, dict):
            if result.get("ok"):
                payout = db.select_one("payouts", id=result["payout_id"])
                if payout:
                    payout["email"] = email
                    return payout
            if result.get("reason") == "insufficient_funds":
                saldo = result.get("balance")
                sufixo = f" Tens {float(saldo):.2f} MT disponiveis." if saldo is not None else ""
                raise BusinessError(f"Saldo insuficiente.{sufixo}", "insufficient_funds")
    except SupabaseError as exc:
        if not _rpc_missing(exc):
            raise

    debit_balance(db, user_id, amount)
    payout_id = secrets.token_hex(16)
    row = {
        "id": payout_id,
        "user_id": user_id,
        "amount_mt": str(amount),
        "method": request.method,
        "phone": request.phone,
        "holder_name": request.holder_name or "",
        "status": PAYOUT_STATUS_PENDING,
        "admin_note": "",
    }
    db.insert("payouts", row)
    row["email"] = email
    return row


def decide_payout(
    db: SupabaseClient,
    *,
    payout_id: str,
    decision: str,
    note: str,
    admin_id: str,
) -> dict:
    try:
        result = db.rpc(
            "settle_payout",
            {
                "p_payout_id": payout_id,
                "p_status": PAYOUT_STATUS_APPROVED if decision == "approve" else PAYOUT_STATUS_REJECTED,
                "p_note": note or "",
                "p_admin_id": admin_id,
            },
        )
        if isinstance(result, dict):
            if not result.get("ok"):
                raise BusinessError(
                    result.get("reason") or "Nao foi possivel actualizar o levantamento.",
                    "settle_failed",
                )
            payout = db.select_one("payouts", id=payout_id) or {}
            return payout
    except SupabaseError as exc:
        if not _rpc_missing(exc):
            raise

    payout = db.select_one("payouts", id=payout_id)
    if not payout:
        raise BusinessError("Levantamento nao encontrado.", "not_found")
    if payout.get("status") != PAYOUT_STATUS_PENDING:
        raise BusinessError("Este levantamento ja foi decidido.", "already_decided")

    new_status = PAYOUT_STATUS_APPROVED if decision == "approve" else PAYOUT_STATUS_REJECTED
    db.update_where(
        "payouts",
        {
            "status": new_status,
            "admin_note": note or "",
            "decided_by": admin_id,
            "decided_at": utcnow_iso(),
        },
        id=payout_id,
        status=PAYOUT_STATUS_PENDING,
    )
    if new_status == PAYOUT_STATUS_REJECTED:
        credit_balance(
            db,
            payout["user_id"],
            normalize_mt(payout.get("amount_mt") or 0),
            reference=f"reembolso:{payout_id[:8]}",
        )
    payout["status"] = new_status
    return payout


# --------------------------------------------------------------------------- #
# Listagens / estatisticas
# --------------------------------------------------------------------------- #
def list_payments(
    db: SupabaseClient, *, user_id: Optional[str] = None, status: str = "", limit: int = 50
) -> list[dict]:
    query = db.table("payments").select("*")
    if user_id:
        query = query.eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    return query.order("created_at").limit(limit).execute() or []


def list_payouts(
    db: SupabaseClient, *, user_id: Optional[str] = None, status: str = "", limit: int = 50
) -> list[dict]:
    query = db.table("payouts").select("*,profiles(email,full_name)")
    if user_id:
        query = query.eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    return query.order("created_at").limit(limit).execute() or []


def list_login_logs(db: SupabaseClient, *, email: str = "", limit: int = 100) -> list[dict]:
    query = db.table("login_logs").select("*")
    if email:
        query = query.eq("email", email)
    return query.order("created_at").limit(limit).execute() or []


def stats(db: SupabaseClient, settings: Settings) -> dict:
    def count(table: str, **filters: Any) -> int:
        query = db.table(table).select("id")
        for column, value in filters.items():
            query = query.eq(column, value)
        rows = query.limit(1000).execute() or []
        return len(rows)

    def sum_field(table: str, field: str, **filters: Any) -> float:
        query = db.table(table).select(field)
        for column, value in filters.items():
            query = query.eq(column, value)
        rows = query.limit(1000).execute() or []
        total = Decimal("0")
        for row in rows:
            total += normalize_mt(row.get(field) or 0)
        return float(total)

    return {
        "users": count("profiles"),
        "payments_pending": count("payments", status="pending"),
        "payments_paid": count("payments", status="paid"),
        "total_credited_mt": sum_field("payments", "credited_mt", status="paid"),
        "payouts_pending": count("payouts", status=PAYOUT_STATUS_PENDING),
        "payouts_approved": count("payouts", status=PAYOUT_STATUS_APPROVED),
        "total_paid_out_mt": sum_field("payouts", "amount_mt", status=PAYOUT_STATUS_APPROVED),
        "rate_mt_per_usdt": float(get_rate(db, settings)),
    }
