"""Autenticacao: registo, login, sessao httpOnly e registo de tentativas.

O navegador pode usar a `SUPABASE_PUBLISHABLE_KEY` com supabase-js e depois
trocar o access token por um cookie em `POST /api/auth/session`. Os endpoints
`/api/auth/signup` e `/api/auth/login` fazem o mesmo do lado do servidor,
tambem com a chave PUBLICA - a `SUPABASE_SECRET_KEY` nunca e usada em login.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import SupabaseClient, SupabaseError
from app.deps import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    cookie_secure,
    current_user,
    db_dep,
    make_session_cookie,
    profile_of,
    session_response,
)
from app.schemas import SessionResponse, SignInRequest, SignUpRequest
from app.security import login_limiter, mask_email, user_cache
from app.services import BusinessError, ensure_profile

log = logging.getLogger("rabudas.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: Transporte HTTP injectavel (os testes trocam por `httpx.MockTransport`).
_auth_transport: httpx.BaseTransport | None = None


def set_auth_transport(transport: httpx.BaseTransport | None) -> None:
    global _auth_transport
    _auth_transport = transport


# --------------------------------------------------------------------------- #
# Registo de tentativas de login
# --------------------------------------------------------------------------- #
def write_login_log(
    db: SupabaseClient,
    request: Request,
    *,
    email: str,
    success: bool,
    reason: str = "",
    user_id: str = "",
) -> None:
    payload = {
        "email": email,
        "success": success,
        "reason": reason[:200],
        "ip": request.client.host if request.client else "",
        "user_agent": (request.headers.get("user-agent") or "")[:300],
        "user_id": user_id or None,
    }
    try:
        db.insert("login_logs", payload)
    except SupabaseError as exc:
        # Falhar o login por causa do log seria pior do que perder o log.
        log.warning("Nao foi possivel gravar login_logs (%s)", exc.status_code)


def ensure_profile_or_http(db: SupabaseClient, user_id: str, email: str, full_name: str = "") -> dict:
    """`ensure_profile` com os erros de negocio traduzidos para HTTP."""
    try:
        return ensure_profile(db, user_id, email, full_name)
    except BusinessError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc

def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _set_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/signup", response_model=SessionResponse)
def signup(
    body: SignUpRequest,
    request: Request,
    response: Response,
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
    secure: bool = Depends(cookie_secure),
) -> SessionResponse:
    key = f"signup:{client_ip(request)}"
    blocked, retry_in = login_limiter.blocked(key)
    if blocked:
        raise HTTPException(status_code=429, detail=f"Muitas tentativas. Espera {retry_in}s.")

    login_limiter.register(key)
    try:
        result = _auth_call(
            settings,
            "POST",
            "/signup",
            json_body={
                "email": body.email,
                "password": body.password,
                "data": {"full_name": body.full_name},
            },
        )
    except SupabaseError as exc:
        write_login_log(db, request, email=body.email, success=False, reason=exc.message[:200])
        raise HTTPException(status_code=_auth_status(exc.status_code), detail=_auth_detail(exc)) from exc

    access_token = result.get("access_token")
    user = result.get("user") or {}
    user_id = user.get("id")
    if not access_token or not user_id:
        raise HTTPException(
            status_code=502,
            detail="Conta criada, mas nao foi devolvida sessao. Faz login para continuar.",
        )

    ensure_profile_or_http(db, user_id, body.email, body.full_name)
    write_login_log(db, request, email=body.email, success=True, reason="signup", user_id=user_id)
    login_limiter.clear(key)

    session = make_session_cookie({"id": user_id, "access_token": access_token}, settings)
    _set_cookie(response, session, secure)
    return session_response(
        {
            "user_id": user_id,
            "email": body.email,
            "full_name": body.full_name,
            "role": "user",
            "balance_mt": 0,
        }
    )


@router.post("/login", response_model=SessionResponse)
def login(
    body: SignInRequest,
    request: Request,
    response: Response,
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
    secure: bool = Depends(cookie_secure),
) -> SessionResponse:
    key = f"login:{client_ip(request)}:{body.email}"
    blocked, retry_in = login_limiter.blocked(key)
    if blocked:
        write_login_log(
            db, request, email=body.email, success=False, reason="rate_limited"
        )
        raise HTTPException(
            status_code=429,
            detail=f"Muitas tentativas de login. Espera {retry_in} segundos.",
        )

    login_limiter.register(key)
    try:
        result = _auth_call(
            settings,
            "POST",
            "/token",
            json_body={"email": body.email, "password": body.password},
            params={"grant_type": "password"},
        )
    except SupabaseError as exc:
        write_login_log(db, request, email=body.email, success=False, reason=exc.message[:200])
        raise HTTPException(
            status_code=_auth_status(exc.status_code),
            detail="Email ou palavra-passe incorrectos."
            if exc.status_code in (400, 401, 403)
            else _auth_detail(exc),
        ) from exc

    access_token = result.get("access_token")
    user = result.get("user") or {}
    user_id = user.get("id")
    if not access_token or not user_id:
        raise HTTPException(status_code=502, detail="Resposta inesperada da Supabase.")

    metadata = user.get("user_metadata") or {}
    profile = ensure_profile_or_http(db, user_id, body.email, metadata.get("full_name", ""))
    write_login_log(db, request, email=body.email, success=True, reason="login", user_id=user_id)
    login_limiter.clear(key)
    log.info("Login ok: %s", mask_email(body.email))

    session = make_session_cookie({"id": user_id, "access_token": access_token}, settings)
    _set_cookie(response, session, secure)
    return session_response(
        {"user_id": user_id, "email": body.email, "role": profile.get("role", "user")}, profile
    )


@router.post("/session", response_model=SessionResponse)
def exchange_session(
    payload: dict,
    request: Request,
    response: Response,
    db: SupabaseClient = Depends(db_dep),
    settings: Settings = Depends(get_settings),
    secure: bool = Depends(cookie_secure),
) -> SessionResponse:
    """Troca um access token obtido no navegador (supabase-js) por cookie httpOnly."""
    access_token = str(payload.get("access_token") or payload.get("accessToken") or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token em falta.")

    try:
        user = db.get_user(access_token)
    except SupabaseError as exc:
        write_login_log(
            db, request, email=str(payload.get("email") or "?"), success=False, reason="token_invalido"
        )
        raise HTTPException(status_code=401, detail="Token invalido.") from exc

    user_id = user.get("id")
    email = user.get("email") or ""
    metadata = user.get("user_metadata") or {}
    profile = ensure_profile_or_http(db, user_id, email, metadata.get("full_name", ""))
    write_login_log(db, request, email=email, success=True, reason="session", user_id=user_id)

    _set_cookie(response, make_session_cookie({"id": user_id, "access_token": access_token}, settings), secure)
    return session_response(
        {"user_id": user_id, "email": email, "role": profile.get("role", "user")}, profile
    )


@router.get("/me", response_model=SessionResponse)
def me(
    user: dict = Depends(current_user),
    db: SupabaseClient = Depends(db_dep),
) -> SessionResponse:
    profile = profile_of(db, user)
    return session_response(user, profile)


@router.post("/logout")
def logout(response: Response) -> JSONResponse:
    user_cache.clear()
    out = JSONResponse({"ok": True})
    out.delete_cookie(SESSION_COOKIE, path="/")
    return out


# --------------------------------------------------------------------------- #
# Chamadas GoTrue com a chave PUBLICA
# --------------------------------------------------------------------------- #
def _auth_call(
    settings: Settings,
    method: str,
    path: str,
    *,
    json_body: dict,
    params: dict | None = None,
) -> dict:
    key = settings.SUPABASE_PUBLISHABLE_KEY
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(
            base_url=settings.supabase_auth_url, timeout=20.0, transport=_auth_transport
        ) as http:
            response = http.request(method, path, json=json_body, params=params, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("Falha de rede ao falar com Supabase Auth: %s", exc)
        raise SupabaseError(502, "Nao foi possivel contactar a Supabase.") from exc

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:  # pragma: no cover
            detail = {"message": response.text[:200]}
        message = (
            detail.get("error_description")
            or detail.get("msg")
            or detail.get("message")
            or detail.get("error")
            or "erro"
        )
        raise SupabaseError(response.status_code, str(message), payload=detail)
    return response.json()


def _auth_status(code: int) -> int:
    if code in (400, 401, 403):
        return 401
    if code == 429:
        return 429
    return 502


def _auth_detail(exc: SupabaseError) -> str:
    message = (exc.message or "").lower()
    if "already registered" in message or "already been registered" in message:
        return "Este email ja esta registado. Faz login."
    if "password" in message and "short" in message:
        return "A palavra-passe tem de ter pelo menos 8 caracteres."
    if "email" in message:
        return "Email invalido."
    return "Nao foi possivel concluir a operacao."
