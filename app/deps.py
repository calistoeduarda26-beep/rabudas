"""Dependencias FastAPI: configuracao, base de dados e autenticacao."""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db import SupabaseClient, SupabaseError, get_supabase
from app.schemas import SessionResponse
from app.security import sign_token, user_cache, verify_token
from app.services import BusinessError, ensure_profile, get_profile

log = logging.getLogger("rabudas.deps")

SESSION_COOKIE = "rabudas_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 horas
COOKIE_SECURE_DEFAULT = False  # activado automaticamente quando BASE_URL usa https


def db_dep(settings: Settings = Depends(get_settings)) -> SupabaseClient:
    return get_supabase(settings)


def request_base_url(request: Request, settings: Settings) -> str:
    """URL publico desta aplicacao.

    Usa `BASE_URL` quando definida; caso contrario deriva do pedido
    (`x-forwarded-host`/`x-forwarded-proto`), que e o comportamento correcto
    atras de um proxy reverso ou em serverless (Vercel, Railway, ...).
    """
    if settings.BASE_URL:
        return settings.BASE_URL.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    forwarded_host = forwarded_host.split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    if not forwarded_host:
        return str(request.base_url).rstrip("/")
    return f"{proto}://{forwarded_host}"


def cookie_secure(request: Request, settings: Settings = Depends(get_settings)) -> bool:
    """Cookie so com `Secure` quando o pedido chega por HTTPS."""
    if settings.ENVIRONMENT == "production":
        return True
    if settings.BASE_URL.lower().startswith("https://"):
        return True
    return (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip() == "https"


def _token_fingerprint(access_token: str) -> str:
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def resolve_session_user(db: SupabaseClient, access_token: str) -> Optional[dict]:
    """Valida um access token da Supabase no servidor e devolve o utilizador."""
    fingerprint = _token_fingerprint(access_token)
    cached = user_cache.get(fingerprint)
    if cached:
        return cached
    try:
        user = db.get_user(access_token)
    except SupabaseError as exc:
        if exc.status_code in (400, 401, 403):
            log.info("Sessao invalida (%s)", exc.status_code)
            return None
        # 5xx e afins nao sao culpa do utilizador: nao fingir "sessao expirada",
        # porque isso esconde uma avaria real (foi o que aconteceu com o apikey).
        log.error("Supabase auth indisponivel ao validar sessao (%s): %s", exc.status_code, exc.message)
        raise
    except Exception as exc:  # rede
        log.warning("Sem acesso ao Supabase Auth para validar a sessao: %s", exc)
        raise
    if not user or not user.get("id"):
        return None
    user_cache.set(fingerprint, user)
    return user


def make_session_cookie(user: dict, settings: Settings) -> str:
    """Cookie assinado: contem o access token da Supabase para revalidacao."""
    return sign_token(
        {
            "uid": user["id"],
            "at": user["access_token"],
        },
        settings.APP_SECRET_KEY.get_secret_value(),
        SESSION_TTL_SECONDS,
    )


def current_user(
    request: Request,
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
) -> dict:
    access_token: Optional[str] = None

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        payload = verify_token(cookie, settings.APP_SECRET_KEY.get_secret_value())
        if payload:
            access_token = payload.get("at")

    if not access_token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            access_token = header[7:].strip()

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessao expirada. Faz login outra vez.",
            headers={"WWW-Authenticate": "Cookie"},
        )

    user = resolve_session_user(db, access_token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessao invalida ou expirada."
        )

    email = user.get("email") or ""
    metadata = user.get("user_metadata") or {}
    try:
        profile = ensure_profile(db, user["id"], email, metadata.get("full_name", ""))
    except BusinessError as exc:
        raise translate_business_error(exc) from exc
    except SupabaseError as exc:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel.") from exc

    return {
        "user_id": user["id"],
        "email": email,
        "full_name": profile.get("full_name") or metadata.get("full_name", ""),
        "role": profile.get("role") or "user",
        "balance_mt": profile.get("balance_mt"),
        "access_token": access_token,
    }


def current_admin(user: dict = Depends(current_user)) -> dict:
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Apenas administradores.")
    return user


def session_response(user: dict, profile: Optional[dict] = None) -> SessionResponse:
    data = profile or {}
    return SessionResponse(
        user_id=user["user_id"],
        email=user["email"],
        full_name=user.get("full_name", ""),
        role=user.get("role", "user"),
        balance_mt=f"{float(data.get('balance_mt') or user.get('balance_mt') or 0):.2f}",
    )


def translate_business_error(exc: BusinessError) -> HTTPException:
    code = status.HTTP_409_CONFLICT
    if exc.code == "not_found":
        code = status.HTTP_404_NOT_FOUND
    elif exc.code in {"np_error", "race"}:
        code = status.HTTP_502_BAD_GATEWAY
    elif exc.code == "schema_missing":
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=code, detail=exc.message)


def profile_of(db: SupabaseClient, user: dict) -> dict:
    try:
        return get_profile(db, user["user_id"])
    except BusinessError as exc:
        raise translate_business_error(exc) from exc
