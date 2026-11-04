"""Cliente da NOWPayments (backend apenas) + verificacao de assinatura IPN.

A `NOWPAYMENTS_API_KEY` so vive aqui e no `.env`. O browser recebe apenas o
`NOWPAYMENTS_PUBLIC_KEY` se for estritamente necessario (ver
`Settings.public_client_config`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("rabudas.nowpayments")

DEFAULT_TIMEOUT = 20.0

#: Estados da NOWPayments -> estado interno da plataforma
STATUS_MAP = {
    "waiting": "pending",
    "confirming": "processing",
    "confirmed": "processing",
    "sending": "processing",
    "partially_paid": "partial",
    "finished": "paid",
    "failed": "failed",
    "refunded": "refunded",
    "expired": "expired",
}

TERMINAL_STATUSES = {"paid", "failed", "refunded", "expired"}


class NOWPaymentsError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"NOWPayments {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def verify_ipn_signature(raw_body: bytes, signature: str, ipn_secret: str) -> bool:
    """Verifica o header `x-nowpayments-sig` (HMAC-SHA512 do JSON canonico).

    A NOWPayments assina `json.dumps(payload, sort_keys=True)` com separadores
    compactos. Compara em tempo constante.
    """
    if not ipn_secret or not signature:
        return False
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (ValueError, UnicodeDecodeError):
        return False
    expected = hmac.new(
        ipn_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def sign_ipn_payload(payload: dict, ipn_secret: str) -> str:
    """Assina um payload IPN (usado nos testes e para validar a configuracao)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        ipn_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512
    ).hexdigest()


class NOWPaymentsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.nowpayments.io",
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise ValueError("NOWPAYMENTS_API_KEY esta vazia.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text[:200])
            except Exception:  # pragma: no cover
                detail = response.text[:200]
            log.warning("NOWPayments %s %s -> %s", method, path, response.status_code)
            raise NOWPaymentsError(response.status_code, str(detail))
        return response.json()

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    def currencies(self) -> dict:
        return self._request("GET", "/v1/currencies")

    def estimate(self, amount: float, currency_from: str, currency_to: str = "usd") -> dict:
        return self._request(
            "GET",
            "/v1/estimate",
            params={
                "amount": amount,
                "currency_from": currency_from,
                "currency_to": currency_to,
            },
        )

    def get_minimum_amount(self, currency_from: str, currency_to: str = "usd") -> dict:
        return self._request(
            "GET",
            "/v1/min-amount",
            params={"currency_from": currency_from, "currency_to": currency_to},
        )

    def create_payment(self, payload: dict) -> dict:
        return self._request("POST", "/v1/payment", json=payload)

    def get_payment(self, payment_id: str | int) -> dict:
        return self._request("GET", f"/v1/payment/{payment_id}")

    def get_payment_by_order_id(self, order_id: str) -> dict:
        return self._request("GET", f"/v1/order/{order_id}")

    def close(self) -> None:
        self._http.close()


_client: Optional[NOWPaymentsClient] = None


def get_nowpayments(settings=None) -> NOWPaymentsClient:  # noqa: ANN001
    global _client
    if _client is None:
        from app.config import get_settings

        cfg = settings or get_settings()
        _client = NOWPaymentsClient(
            cfg.NOWPAYMENTS_API_KEY.get_secret_value(), base_url=cfg.NOWPAYMENTS_BASE_URL
        )
    return _client


def set_nowpayments_client(client: Optional[NOWPaymentsClient]) -> None:
    """Usado nos testes para injetar um cliente com transporte falso."""
    global _client
    _client = client


def map_status(nowpayments_status: str) -> str:
    return STATUS_MAP.get((nowpayments_status or "").lower(), "unknown")
