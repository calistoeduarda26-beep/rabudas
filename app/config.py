"""Configuracao central da aplicacao.

Todas as variaveis vem do ambiente / ficheiro `.env` (python-dotenv via
pydantic-settings). Nenhuma chave e escrita em codigo-fonte.

Regras:
- O arranque FALHA (exit 1) se faltar alguma variavel obrigatoria ou se
  alguma contiver um placeholder nao preenchido ("<...>").
- `SUPABASE_SECRET_KEY` e `NOWPAYMENTS_API_KEY` sao `SecretStr`: nunca
  aparecem em `repr()`, em logs ou em respostas da API.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

#: Variaveis que tem de existir e ter valor real para a app arrancar.
#: `BASE_URL` e opcional: quando nao esta definida, o URL publico e derivado
#: de cada pedido (x-forwarded-host), o que e o correcto em serverless.
REQUIRED_VARS: tuple[str, ...] = (
    "NOWPAYMENTS_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "APP_SECRET_KEY",
)

#: Variaveis opcionais que, se nao estiverem preenchidas, degradam funcionalidade
#: (o IPN da NOWPayments fica desativado) em vez de impedir o arranque.
OPTIONAL_WARN_VARS: tuple[str, ...] = ("NOWPAYMENTS_IPN_SECRET",)


class ConfigError(RuntimeError):
    """Erro de configuracao com mensagem segura (nomes de variaveis, nunca valores)."""


def _looks_like_placeholder(value: str) -> bool:
    value = value.strip()
    return (
        value == ""
        or value.startswith("<")
        or value.lower() in {"changeme", "change-me", "todo", "none", "null"}
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- NOWPayments (uso exclusivo do backend) ---
    NOWPAYMENTS_API_KEY: SecretStr = SecretStr("")
    NOWPAYMENTS_PUBLIC_KEY: str = ""
    NOWPAYMENTS_IPN_SECRET: SecretStr = SecretStr("")
    NOWPAYMENTS_BASE_URL: str = "https://api.nowpayments.io"

    # --- Supabase ---
    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""
    SUPABASE_SECRET_KEY: SecretStr = SecretStr("")

    # --- App ---
    APP_SECRET_KEY: SecretStr = SecretStr("")
    BASE_URL: str = ""
    RATE_MT_PER_USDT: float = Field(default=80.0, gt=0)

    # --- Servidor ---
    ENVIRONMENT: str = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: str = "*"

    # ------------------------------------------------------------------ #
    # Validacao
    # ------------------------------------------------------------------ #
    @field_validator(
        "NOWPAYMENTS_API_KEY",
        "SUPABASE_SECRET_KEY",
        "APP_SECRET_KEY",
        mode="before",
    )
    @classmethod
    def _no_placeholder_secrets(cls, v):  # noqa: ANN001, ANN206
        if v is None:
            return ""
        if isinstance(v, str) and _looks_like_placeholder(v):
            return ""
        return v

    @field_validator("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_URL", "BASE_URL", mode="before")
    @classmethod
    def _no_placeholder_plain(cls, v):  # noqa: ANN001, ANN206
        if v is None:
            return ""
        if isinstance(v, str) and _looks_like_placeholder(v):
            return ""
        return v

    @field_validator("APP_SECRET_KEY")
    @classmethod
    def _app_secret_length(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value() and len(v.get_secret_value()) < 32:
            raise ValueError(
                "APP_SECRET_KEY demasiado curta: usa pelo menos 32 caracteres "
                '(gerar com: python -c "import secrets;print(secrets.token_urlsafe(48)[:64])")'
            )
        return v

    @field_validator("SUPABASE_URL", "BASE_URL", "NOWPAYMENTS_BASE_URL")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return (v or "").strip().rstrip("/")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @property
    def cors_origin_list(self) -> List[str]:
        raw = (self.CORS_ORIGINS or "").strip()
        if raw in ("", "*"):
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def ipn_enabled(self) -> bool:
        return bool(self.NOWPAYMENTS_IPN_SECRET.get_secret_value())

    @property
    def supabase_rest_url(self) -> str:
        return f"{self.SUPABASE_URL}/rest/v1"

    @property
    def supabase_auth_url(self) -> str:
        return f"{self.SUPABASE_URL}/auth/v1"

    def missing_required(self) -> List[str]:
        """Nomes (nunca valores) das variaveis obrigatorias em falta."""
        values = {
            "NOWPAYMENTS_API_KEY": self.NOWPAYMENTS_API_KEY.get_secret_value(),
            "SUPABASE_URL": self.SUPABASE_URL,
            "SUPABASE_PUBLISHABLE_KEY": self.SUPABASE_PUBLISHABLE_KEY,
            "SUPABASE_SECRET_KEY": self.SUPABASE_SECRET_KEY.get_secret_value(),
            "APP_SECRET_KEY": self.APP_SECRET_KEY.get_secret_value(),
            "RATE_MT_PER_USDT": str(self.RATE_MT_PER_USDT),
        }
        return [name for name in REQUIRED_VARS if not str(values.get(name, "")).strip()]

    def public_client_config(self) -> dict:
        """Unico conjunto de valores que o navegador tem autorizacao a receber.

        NUNCA inclui SUPABASE_SECRET_KEY nem NOWPAYMENTS_API_KEY.
        """
        return {
            "supabaseUrl": self.SUPABASE_URL,
            "supabasePublishableKey": self.SUPABASE_PUBLISHABLE_KEY,
            "baseUrl": self.BASE_URL,
            "rateMtPerUsdt": self.RATE_MT_PER_USDT,
            "ipnEnabled": self.ipn_enabled,
        }

    def masked_summary(self) -> dict:
        """Resumo seguro para logs de arranque: mostra apenas prefixos."""

        def mask(secret: SecretStr) -> str:
            v = secret.get_secret_value()
            if not v:
                return "<vazio>"
            return f"{v[:4]}…{v[-2:]} ({len(v)} chars)"

        return {
            "NOWPAYMENTS_API_KEY": mask(self.NOWPAYMENTS_API_KEY),
            "SUPABASE_SECRET_KEY": mask(self.SUPABASE_SECRET_KEY),
            "SUPABASE_PUBLISHABLE_KEY": (
                f"{self.SUPABASE_PUBLISHABLE_KEY[:16]}…" if self.SUPABASE_PUBLISHABLE_KEY else "<vazio>"
            ),
            "APP_SECRET_KEY": mask(self.APP_SECRET_KEY),
            "SUPABASE_URL": self.SUPABASE_URL,
            "BASE_URL": self.BASE_URL,
            "RATE_MT_PER_USDT": self.RATE_MT_PER_USDT,
        }


def load_settings(*, strict: bool = True, env_file: str | None = None) -> Settings:
    """Carrega as definicoes. Com `strict=True` falha se faltar algo obrigatorio."""
    target = env_file if env_file is not None else (str(ENV_FILE) if ENV_FILE.exists() else None)
    try:
        settings = Settings(_env_file=target)  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"Erro ao ler .env / variaveis de ambiente: {exc}") from exc

    missing = settings.missing_required()
    if strict and missing:
        where = ENV_FILE if ENV_FILE.exists() else f"{ENV_FILE} (nao existe - copia .env.example)"
        raise ConfigError(
            "Variaveis obrigatorias em falta ou com placeholder: "
            + ", ".join(missing)
            + f". Verifica {where}"
        )
    return settings


def write_env_template(path: str = ".env") -> str:  # pragma: no cover - utilitario
    import shutil

    source = PROJECT_ROOT / ".env.example"
    shutil.copyfile(source, path)
    return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings(strict=True)


def main() -> int:
    """`python -m app.config` -> valida a configuracao sem imprimir segredos."""
    from app.logging_conf import setup_logging

    try:
        settings = load_settings(strict=True)
    except ConfigError as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.ENVIRONMENT)
    import logging

    log = logging.getLogger("rabudas.config")
    log.info("Configuracao OK")
    for key, value in settings.masked_summary().items():
        log.info("  %-26s %s", key, value)
    for name in OPTIONAL_WARN_VARS:
        if not getattr(settings, name).get_secret_value():
            log.warning("  %-26s <vazio> - funcionalidade degradada (ver README)", name)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
