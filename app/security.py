"""Seguranca: sessoes assinadas, rate limiting e regras de negocio de valores.

Tudo aqui usa `APP_SECRET_KEY` (do `.env`, nunca do codigo).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

log = logging.getLogger("rabudas.security")

# --------------------------------------------------------------------------- #
# Tokens assinados (sessao httpOnly)
# --------------------------------------------------------------------------- #
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def sign_token(payload: dict, secret: str, ttl_seconds: int) -> str:
    """Cria `base64(payload).assinatura` com expiracao."""
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    body["iat"] = int(time.time())
    raw = _b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    digest = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    return f"{raw}.{_b64encode(digest)}"


def verify_token(token: str, secret: str) -> Optional[dict]:
    """Devolve o payload se a assinatura for valida e nao tiver expirado."""
    if not token or "." not in token:
        return None
    raw, _, signature = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest()
    try:
        provided = _b64decode(signature)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64decode(raw).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# --------------------------------------------------------------------------- #
# Rate limiting de logins (memoria do processo)
# --------------------------------------------------------------------------- #
@dataclass
class RateLimiter:
    max_attempts: int = 5
    window_seconds: int = 300
    _hits: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def _prune(self, key: str, now: float) -> None:
        window = self._hits[key]
        while window and now - window[0] > self.window_seconds:
            window.popleft()

    def register(self, key: str) -> None:
        self._prune(key, time.time())
        self._hits[key].append(time.time())

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def blocked(self, key: str) -> tuple[bool, int]:
        now = time.time()
        self._prune(key, now)
        attempts = len(self._hits[key])
        return attempts >= self.max_attempts, max(0, self.window_seconds - int(now - (self._hits[key][0] if self._hits[key] else now)))


login_limiter = RateLimiter()


# --------------------------------------------------------------------------- #
# Dinheiro: Meticais (MT) <-> USDT
# --------------------------------------------------------------------------- #
TWO_PLACES = Decimal("0.01")


def to_decimal(value) -> Decimal:  # noqa: ANN001
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def mt_to_usdt(amount_mt, rate: float) -> Decimal:  # noqa: ANN001
    """Converte Meticais em USDT usando a taxa fixa da plataforma."""
    return (to_decimal(amount_mt) / to_decimal(rate)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def usdt_to_mt(amount_usdt, rate: float) -> Decimal:  # noqa: ANN001
    return (to_decimal(amount_usdt) * to_decimal(rate)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def normalize_mt(value) -> Decimal:  # noqa: ANN001
    """Arredonda um valor em MT para 2 casas decimais."""
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


_PHONE_RE = re.compile(r"^\+?258(8[2-7]\d{7})$")


def normalize_mz_phone(value: str) -> str:
    """Normaliza numeros mocambicanos para o formato internacional +258XXXXXXXX.

    Aceita 84/85 (M-Pesa), 86/87 (e-Mola) e 82/83 (Tmcel).
    """
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 9 and digits.startswith("8"):
        digits = f"258{digits}"
    match = _PHONE_RE.match(f"+{digits}" if not digits.startswith("+") else digits)
    if not match:
        raise ValueError("Numero de telefone invalido. Use o formato 84XXXXXXX ou +25884XXXXXXX.")
    return f"+258{match.group(1)}"


def mask_phone(phone: str) -> str:
    """Usado em logs e em respostas publicas."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return "***"
    return f"{digits[:5]}***{digits[-2:]}"


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}"


# --------------------------------------------------------------------------- #
# Cache curta de validacao de sessoes Supabase
# --------------------------------------------------------------------------- #
@dataclass
class TTLCache:
    ttl_seconds: int = 45
    _data: dict[str, tuple[float, dict]] = field(default_factory=dict)

    def get(self, key: str) -> Optional[dict]:
        entry = self._data.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: dict) -> None:
        self._data[key] = (time.time() + self.ttl_seconds, value)

    def drop(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


user_cache = TTLCache()
