"""Configuracao de logging com redacao obrigatoria de segredos.

Qualquer mensagem registada passa por `RedactFilter`, que substitui os valores
reais das chaves por `***REDACTED***`. Assim, mesmo que alguem faca
`log.info(settings.NOWPAYMENTS_API_KEY.get_secret_value())`, a chave nao chega
aos logs.
"""

from __future__ import annotations

import logging
import sys

REDACTED = "***REDACTED***"

_settings_secrets: list[str] = []
_setup_done = False


def register_secrets(*values: str) -> None:
    """Regista strings que devem ser apagadas de qualquer mensagem de log."""
    for value in values:
        if value and len(value) >= 8 and value not in _settings_secrets:
            _settings_secrets.append(value)
    # Mais longas primeiro, para mascarar chaves completas antes das curtas.
    _settings_secrets.sort(key=len, reverse=True)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - args mal formados
            return True
        for secret in _settings_secrets:
            if secret in message:
                message = message.replace(secret, REDACTED)
        if message != record.getMessage():
            record.msg = message
            record.args = ()
        return True


def setup_logging(environment: str = "development") -> None:
    """Configura o logging raiz uma unica vez por processo."""
    global _setup_done
    if _setup_done:
        return

    level = logging.INFO if environment != "test" else logging.WARNING
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
    )
    handler.addFilter(RedactFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Os loggers do uvicorn herdam do root, mas garantimos o filtro tambem neles.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.addFilter(RedactFilter())

    _setup_done = True
