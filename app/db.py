"""Cliente Supabase (PostgREST + GoTrue) para uso EXCLUSIVO do backend.

Usa a `SUPABASE_SECRET_KEY` (sb_secret_...) que e privada: este modulo nunca e
importado por templates, JS do browser ou respostas da API. As operacoes que o
navegador faz (login/registo) usam a chave publica `sb_publishable_...`.

Implementado diretamente sobre HTTP (httpx) para nao depender do SDK pesado e
para ser facil de testar com `httpx.MockTransport`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

import httpx

from app.config import Settings

log = logging.getLogger("rabudas.db")

DEFAULT_TIMEOUT = 15.0


class SupabaseError(RuntimeError):
    def __init__(self, status_code: int, message: str, *, payload: Any = None) -> None:
        super().__init__(f"Supabase {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


def _format_filter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    text = str(value)
    if any(char in text for char in ',.()"') or text == "":
        return '"' + text.replace('"', '\\"') + '"'
    return text


class TableQuery:
    """Construtor de queries estilo PostgREST (encadeavel)."""

    def __init__(self, client: "SupabaseClient", table: str) -> None:
        self._client = client
        self._table = table
        self._params: list[tuple[str, str]] = []
        self._method = "GET"
        self._json: Any = None
        self._single = False
        self._maybe_single = False
        self._returning = False

    # -- filtros -------------------------------------------------------- #
    def select(self, columns: str = "*") -> "TableQuery":
        self._method = "GET"
        self._params.append(("select", columns))
        return self

    def eq(self, column: str, value: Any) -> "TableQuery":
        self._params.append((column, f"eq.{_format_filter_value(value)}"))
        return self

    def neq(self, column: str, value: Any) -> "TableQuery":
        self._params.append((column, f"neq.{_format_filter_value(value)}"))
        return self

    def in_(self, column: str, values: Iterable[Any]) -> "TableQuery":
        formatted = ",".join(_format_filter_value(v) for v in values)
        self._params.append((column, f"in.({formatted})"))
        return self

    def gte(self, column: str, value: Any) -> "TableQuery":
        self._params.append((column, f"gte.{_format_filter_value(value)}"))
        return self

    def lte(self, column: str, value: Any) -> "TableQuery":
        self._params.append((column, f"lte.{_format_filter_value(value)}"))
        return self

    def ilike(self, column: str, pattern: str) -> "TableQuery":
        self._params.append((column, f"ilike.{_format_filter_value(pattern)}"))
        return self

    def order(self, column: str, *, desc: bool = True) -> "TableQuery":
        self._params.append(("order", f"{column}.desc" if desc else f"{column}.asc"))
        return self

    def limit(self, count: int) -> "TableQuery":
        self._params.append(("limit", str(int(count))))
        return self

    # -- modos ------------------------------------------------------------ #
    def single(self) -> "TableQuery":
        self._single = True
        return self

    def maybe_single(self) -> "TableQuery":
        self._maybe_single = True
        return self

    def insert(self, payload: Any) -> "TableQuery":
        self._method = "POST"
        self._json = payload
        self._returning = True
        return self

    def update(self, payload: Any) -> "TableQuery":
        self._method = "PATCH"
        self._json = payload
        self._returning = True
        return self

    def delete(self) -> "TableQuery":
        self._method = "DELETE"
        self._returning = True
        return self

    # -- execucao --------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._single:
            headers["Accept"] = "application/vnd.pgrst.object+json"
        if self._returning:
            headers["Prefer"] = "return=representation"
        return headers

    def execute(self) -> Any:
        prefer = []
        if self._returning:
            prefer.append("return=representation")
        response = self._client.request(
            self._method,
            f"/{self._table}",
            params=self._params,
            json=self._json if self._json is not None else None,
            headers=self._headers(),
            prefer=prefer,
        )
        if self._method == "GET" and response.status_code == 406 and self._maybe_single:
            return None
        if self._single or self._maybe_single:
            return response.json() if response.content else None
        return response.json()


class SupabaseClient:
    def __init__(
        self,
        base_url: str,
        secret_key: str,
        *,
        publishable_key: str = "",
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not secret_key:
            raise ValueError("SUPABASE_SECRET_KEY esta vazia - o backend nao pode iniciar.")
        self.base_url = base_url.rstrip("/")
        self.secret_key = secret_key
        self.publishable_key = publishable_key
        self._http = httpx.Client(
            base_url=f"{self.base_url}/rest/v1",
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ #
    # HTTP base
    # ------------------------------------------------------------------ #
    def _auth_headers(self, token: Optional[str] = None) -> dict[str, str]:
        key = token or self.secret_key
        return {"apikey": key, "Authorization": f"Bearer {key}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        headers: Optional[dict[str, str]] = None,
        prefer: Optional[list[str]] = None,
        token: Optional[str] = None,
    ) -> httpx.Response:
        request_headers = self._auth_headers(token)
        if prefer:
            request_headers["Prefer"] = ",".join(prefer)
        if headers:
            request_headers.update(headers)

        response = self._http.request(
            method, path, params=params, json=json, headers=request_headers
        )
        if response.status_code >= 400:
            message = self._error_message(response)
            log.warning("Supabase %s %s -> %s", method, path, response.status_code)
            raise SupabaseError(response.status_code, message, payload=self._safe_json(response))
        return response

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:  # pragma: no cover
            return None

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                return str(data.get("message") or data.get("error") or data)
            return str(data)
        except Exception:
            return response.text[:200] or "sem corpo"

    # ------------------------------------------------------------------ #
    # Dados (usa a chave secreta -> ignora RLS)
    # ------------------------------------------------------------------ #
    def table(self, name: str) -> TableQuery:
        return TableQuery(self, name)

    def rpc(self, function: str, payload: Optional[dict] = None) -> Any:
        response = self.request("POST", f"/rpc/{function}", json=payload or {})
        return self._safe_json(response)

    def insert(self, table: str, payload: Any) -> Any:
        return self.table(table).insert(payload).execute()

    def upsert(self, table: str, payload: Any) -> Any:
        """Insert-or-update (requer chave primaria/unica definida na tabela)."""
        response = self.request(
            "POST",
            f"/{table}",
            json=payload,
            headers={
                "Prefer": "return=representation,resolution=merge-duplicates",
                "Content-Type": "application/json",
            },
        )
        return self._safe_json(response)

    def select_one(self, table: str, **filters: Any) -> Optional[dict]:
        query = self.table(table).select("*")
        for column, value in filters.items():
            query = query.eq(column, value)
        query = query.limit(1)
        rows = query.execute()
        return rows[0] if rows else None

    def update_where(self, table: str, payload: dict, **filters: Any) -> list:
        query = self.table(table).update(payload)
        for column, value in filters.items():
            query = query.eq(column, value)
        return query.execute()

    # ------------------------------------------------------------------ #
    # Auth (GoTrue)
    # ------------------------------------------------------------------ #
    def auth_request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None, token: str = ""
    ) -> Any:
        key = token or self.secret_key
        headers = {"apikey": key, "Authorization": f"Bearer {token or self.secret_key}"}
        with httpx.Client(
            base_url=f"{self.base_url}/auth/v1", timeout=DEFAULT_TIMEOUT, transport=self._http._transport
        ) as auth_http:
            response = auth_http.request(method, path, json=json_body, params=params, headers=headers)
        if response.status_code >= 400:
            message = self._error_message(response)
            log.warning("Supabase auth %s %s -> %s", method, path, response.status_code)
            raise SupabaseError(response.status_code, message, payload=self._safe_json(response))
        return self._safe_json(response)

    def sign_up(self, email: str, password: str, data: Optional[dict] = None) -> dict:
        body = {"email": email, "password": password, "data": data or {}}
        return self.auth_request("POST", "/signup", json_body=body)

    def sign_in_with_password(self, email: str, password: str) -> dict:
        body = {"email": email, "password": password}
        return self.auth_request(
            "POST", "/token", json_body=body, params={"grant_type": "password"}
        )

    def get_user(self, access_token: str) -> dict:
        """Valida um access token no servidor (nao precisa do JWT secret)."""
        return self.auth_request("GET", "/user", token=access_token)

    def close(self) -> None:
        self._http.close()


_client: Optional[SupabaseClient] = None


def get_supabase(settings: Optional[Settings] = None) -> SupabaseClient:
    """Singleton do cliente Supabase do backend."""
    global _client
    if _client is None:
        from app.config import get_settings

        cfg = settings or get_settings()
        _client = SupabaseClient(
            cfg.SUPABASE_URL,
            cfg.SUPABASE_SECRET_KEY.get_secret_value(),
            publishable_key=cfg.SUPABASE_PUBLISHABLE_KEY,
        )
    return _client


def set_supabase_client(client: Optional[SupabaseClient]) -> None:
    """Usado nos testes para injetar um cliente com transporte falso."""
    global _client
    _client = client


def rows_to_json(rows: Any) -> Any:
    """Normaliza respostas do PostgREST para JSON serializavel."""
    return json.loads(json.dumps(rows, default=str))
