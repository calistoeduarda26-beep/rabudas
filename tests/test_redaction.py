"""As chaves nunca podem chegar aos logs."""

from __future__ import annotations

import logging

import pytest

from app.logging_conf import REDACTED, RedactFilter, register_secrets, setup_logging


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def _logger(name: str) -> tuple[logging.Logger, _Capture]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    capture = _Capture()
    capture.addFilter(RedactFilter())
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    return logger, capture


def test_segredo_e_apagado_da_mensagem():
    register_secrets("sb_secret_SUPERSECRETA123")
    logger, capture = _logger("test.redact1")
    logger.info("a usar a chave sb_secret_SUPERSECRETA123 para ligar")
    assert capture.records == [f"a usar a chave {REDACTED} para ligar"]
    assert "SUPERSECRETA" not in capture.records[0]


def test_segredo_em_args_tambem_e_apagado():
    register_secrets("np_api_key_MUITO_SECRETA")
    logger, capture = _logger("test.redact2")
    logger.info("chave=%s url=%s", "np_api_key_MUITO_SECRETA", "https://api.nowpayments.io")
    assert "MUITO_SECRETA" not in capture.records[0]
    assert "https://api.nowpayments.io" in capture.records[0]


def test_mensagem_sem_segredo_nao_muda():
    logger, capture = _logger("test.redact3")
    logger.info("pedido %s processado", "RBD-1234")
    assert capture.records == ["pedido RBD-1234 processado"]


def test_chaves_reais_do_env_nao_aparecem_em_logs():
    from app.config import ENV_FILE, load_settings

    if not ENV_FILE.exists():
        pytest.skip(".env nao esta no git (gitignored); corre 'cp .env.example .env'")
    settings = load_settings(strict=True)
    register_secrets(
        settings.NOWPAYMENTS_API_KEY.get_secret_value(),
        settings.SUPABASE_SECRET_KEY.get_secret_value(),
        settings.APP_SECRET_KEY.get_secret_value(),
    )
    logger, capture = _logger("test.redact4")
    logger.info(
        "np=%s sb=%s app=%s",
        settings.NOWPAYMENTS_API_KEY.get_secret_value(),
        settings.SUPABASE_SECRET_KEY.get_secret_value(),
        settings.APP_SECRET_KEY.get_secret_value(),
    )
    saida = capture.records[0]
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value() not in saida
    assert settings.SUPABASE_SECRET_KEY.get_secret_value() not in saida
    assert settings.APP_SECRET_KEY.get_secret_value() not in saida
    assert saida.count(REDACTED) == 3


def test_setup_logging_instala_filtro_no_root():
    setup_logging("test")
    root = logging.getLogger()
    assert root.handlers, "o logger raiz tem de ter um handler"
    assert any(isinstance(f, RedactFilter) for f in root.handlers[0].filters)
