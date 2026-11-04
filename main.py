"""Ponto de entrada da API rabudas (FastAPI).

Arranque:
    python main.py            # ou: uvicorn main:app --reload --host 0.0.0.0 --port 8000

Se faltar alguma variavel obrigatoria do `.env`, o processo termina com codigo 1
e uma mensagem que lista apenas os NOMES das variaveis em falta.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ConfigError, Settings, get_settings, load_settings
from app.db import SupabaseError
from app.logging_conf import register_secrets, setup_logging
from app.routers import admin, auth, payments, payouts, webhooks

log = logging.getLogger("rabudas")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _register_secret_redaction(settings: Settings) -> None:
    """Garante que nenhuma chave aparece em logs, mesmo por engano."""
    register_secrets(
        settings.NOWPAYMENTS_API_KEY.get_secret_value(),
        settings.NOWPAYMENTS_API_KEY.get_secret_value().replace("-", ""),
        settings.SUPABASE_SECRET_KEY.get_secret_value(),
        settings.APP_SECRET_KEY.get_secret_value(),
        settings.SUPABASE_PUBLISHABLE_KEY,
        settings.NOWPAYMENTS_IPN_SECRET.get_secret_value(),
        settings.NOWPAYMENTS_PUBLIC_KEY,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    settings = get_settings()
    log.info("rabudas a arrancar em %s (%s)", settings.BASE_URL, settings.ENVIRONMENT)
    for key, value in settings.masked_summary().items():
        log.info("  %-26s %s", key, value)
    if not settings.ipn_enabled:
        log.warning(
            "NOWPAYMENTS_IPN_SECRET vazio: creditos automaticos de deposito ficam desactivados."
        )
    yield
    log.info("rabudas a terminar")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    setup_logging(cfg.ENVIRONMENT)
    _register_secret_redaction(cfg)

    app = FastAPI(
        title="rabudas API",
        version="1.0.0",
        description="Depositos em cripto (NOWPayments) e levantamentos em Meticais.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {
            "status": "ok",
            "service": "rabudas",
            "environment": cfg.ENVIRONMENT,
            "rate_mt_per_usdt": cfg.RATE_MT_PER_USDT,
            "ipn_enabled": cfg.ipn_enabled,
        }

    @app.get("/api/config", tags=["meta"])
    def public_config() -> dict:
        """Unica configuracao entregue ao navegador - so valores publicos."""
        return cfg.public_client_config()

    @app.exception_handler(SupabaseError)
    async def supabase_error_handler(request: Request, exc: SupabaseError) -> JSONResponse:  # noqa: ARG001
        log.error("Erro Supabase em %s: %s", request.url.path, exc.message)
        return JSONResponse(
            status_code=503 if exc.status_code >= 500 else exc.status_code,
            content={"detail": "Base de dados indisponivel."},
        )

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error_handler(request: Request, exc: httpx.HTTPError) -> JSONResponse:  # noqa: ARG001
        log.error("Servico externo indisponivel em %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "Servico externo indisponivel (Supabase ou NOWPayments)."},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Erro nao tratado em %s: %s", request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "Erro interno do servidor."})

    app.include_router(auth.router)
    app.include_router(payments.router)
    app.include_router(payouts.router)
    app.include_router(admin.router)
    app.include_router(webhooks.router)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def main() -> int:
    try:
        settings = load_settings(strict=True)
    except ConfigError as exc:
        print(f"\n[ERRO DE CONFIGURACAO] {exc}\n", file=sys.stderr)
        print("Passos: cp .env.example .env  e preenche os valores (ver README.md).", file=sys.stderr)
        return 1

    setup_logging(settings.ENVIRONMENT)
    import uvicorn

    uvicorn.run(
        "main:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.ENVIRONMENT == "development",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
