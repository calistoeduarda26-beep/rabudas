"""Dobras de teste: Supabase (PostgREST + GoTrue) e NOWPayments em memoria.

Servem para correr o codigo REAL de `app/services.py`, `app/db.py` e dos
routers sem tocar em servicos externos.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable
from urllib.parse import parse_qsl

import httpx


# --------------------------------------------------------------------------- #
# Supabase falsa
# --------------------------------------------------------------------------- #
class FakeSupabase:
    def __init__(self, *, rpc_enabled: bool = True, seed: dict[str, list[dict]] | None = None) -> None:
        self.tables: dict[str, list[dict]] = {
            "profiles": [],
            "payments": [],
            "payouts": [],
            "login_logs": [],
            "settings": [{"key": "rate_mt_per_usdt", "value": "80"}],
        }
        if seed:
            for table, rows in seed.items():
                self.tables.setdefault(table, []).extend(rows)
        self.rpc_enabled = rpc_enabled
        # API keys validas do "projecto". O GoTrue real valida o header apikey
        # contra elas; a dobra anterior ignorava-o e foi isso que deixou passar
        # o bug de mandar o JWT do utilizador no apikey.
        self.api_keys = {"sb_secret_teste", "sb_publishable_teste"}
        self.auth_requests: list[tuple[str, str, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.users: dict[str, dict] = {}          # access_token -> user
        self.credentials: dict[str, str] = {}     # email -> password
        self.log_seq = 0

    # -- helpers ------------------------------------------------------------ #
    def client(self) -> Any:
        from app.db import SupabaseClient

        return SupabaseClient(
            "https://fake.supabase.co", "sb_secret_teste", publishable_key="sb_publishable_teste",
            transport=httpx.MockTransport(self.handle),
        )

    def add_user(self, email: str, password: str = "password123", user_id: str = "") -> dict:
        user_id = user_id or str(uuid.uuid4())
        token = f"token-{user_id[:8]}"
        user = {"id": user_id, "email": email, "user_metadata": {"full_name": email.split("@")[0]}}
        self.users[token] = user
        self.credentials[email] = password
        return {"access_token": token, "user": user}

    @staticmethod
    def _equal(actual: str, value: str) -> bool:
        """Compara como o Postgres: numericos por valor, o resto por texto."""
        if actual == value:
            return True
        try:
            return float(actual) == float(value)
        except (TypeError, ValueError):
            return False

    @classmethod
    def _matches(cls, row: dict, filters: dict[str, str]) -> bool:
        for column, expression in filters.items():
            op, _, value = expression.partition(".")
            # O PostgREST aceita valores entre aspas; remove-as antes de comparar.
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            actual = str(row.get(column))
            if op == "eq" and not cls._equal(actual, value):
                return False
            if op == "neq" and cls._equal(actual, value):
                return False
            if op == "gte" and float(actual or 0) < float(value or 0):
                return False
            if op == "lte" and float(actual or 0) > float(value or 0):
                return False
            if op == "in" and actual.strip('"') not in value.strip("()").replace('"', "").split(","):
                return False
        return True

    @staticmethod
    def _parse_filters(params: list[tuple[str, str]]) -> dict[str, str]:
        reserved = {"select", "order", "limit", "offset", "on_conflict"}
        return {k: v for k, v in params if k not in reserved}

    # -- transporte --------------------------------------------------------- #
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))

        if path.startswith("/auth/v1"):
            return self._handle_auth(request)
        if not path.startswith("/rest/v1"):
            return httpx.Response(404, json={"message": f"sem rota {path}"})

        remainder = path[len("/rest/v1") :]
        if remainder.startswith("/rpc/"):
            return self._handle_rpc(remainder.split("/rpc/")[1], json.loads(request.content or b"{}"))

        table = remainder.lstrip("/")
        if table not in self.tables:
            # Mensagem igual a do PostgREST real (PGRST205), para o codigo
            # que detecta "schema por correr" ser exercitado a serio.
            return httpx.Response(
                404,
                json={
                    "code": "PGRST205",
                    "message": f"Could not find the table 'public.{table}' in the schema cache",
                    "hint": "Perhaps you meant to find the table ...",
                    "details": None,
                },
            )

        params = list(request.url.params.multi_items())
        filters = self._parse_filters(params)

        if request.method == "GET":
            rows = [r for r in self.tables[table] if self._matches(r, filters)]
            order = dict(params).get("order")
            if order:
                column, _, direction = order.partition(".")
                rows = sorted(rows, key=lambda r: str(r.get(column) or ""), reverse=direction != "asc")
            limit = dict(params).get("limit")
            if limit:
                rows = rows[: int(limit)]
            accept = request.headers.get("accept", "")
            if "vnd.pgrst.object" in accept:
                if not rows:
                    return httpx.Response(406, json={"message": "sem linhas"})
                return httpx.Response(200, json=rows[0])
            return httpx.Response(200, json=rows)

        payload = json.loads(request.content or b"{}")
        rows = payload if isinstance(payload, list) else [payload]

        if request.method == "POST":
            prefer = request.headers.get("prefer", "")
            if "merge-duplicates" in prefer:  # upsert
                out = []
                for row in rows:
                    key, value = next(iter(row.items()))
                    existing = next((r for r in self.tables[table] if str(r.get(key)) == str(value)), None)
                    if existing:
                        existing.update(row)
                        out.append(existing)
                    else:
                        self.tables[table].append(dict(row))
                        out.append(row)
                return httpx.Response(201, json=out)
            for row in rows:
                if table == "login_logs":
                    self.log_seq += 1
                    row = {"id": self.log_seq, **row}
                if table == "payouts":
                    row.setdefault("created_at", "2026-01-01T00:00:00+00:00")
                self.tables[table].append(dict(row))
            return httpx.Response(201, json=rows)

        if request.method == "PATCH":
            updated = []
            for row in self.tables[table]:
                if self._matches(row, filters):
                    row.update(payload)
                    updated.append(row)
            if not updated and "vnd.pgrst.object" in request.headers.get("accept", ""):
                return httpx.Response(406, json={"message": "sem linhas"})
            return httpx.Response(200, json=updated)

        return httpx.Response(405, json={"message": "metodo nao suportado"})

    # -- auth --------------------------------------------------------------- #
    def _handle_auth(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}

        apikey = request.headers.get("apikey", "")
        authorization = request.headers.get("authorization", "")
        self.auth_requests.append((path, apikey, authorization))
        if apikey not in self.api_keys:
            return httpx.Response(
                401, json={"code": "invalid_api_key", "message": "Invalid API key"}
            )

        if path.endswith("/signup"):
            email = body.get("email", "")
            if email in self.credentials:
                return httpx.Response(422, json={"msg": "User already registered"})
            return httpx.Response(200, json=self.add_user(email, body.get("password", "")))

        if path.endswith("/token"):
            email, password = body.get("email"), body.get("password")
            if self.credentials.get(email) != password:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid login credentials"})
            token = next(t for t, u in self.users.items() if u["email"] == email)
            return httpx.Response(200, json={"access_token": token, "user": self.users[token]})

        if path.endswith("/user"):
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            user = self.users.get(token)
            if not user:
                return httpx.Response(401, json={"message": "invalid claim: missing sub claim"})
            return httpx.Response(200, json=user)

        return httpx.Response(404, json={"message": "sem rota auth"})

    # -- funcoes SQL -------------------------------------------------------- #
    def _handle_rpc(self, name: str, args: dict) -> httpx.Response:
        if not self.rpc_enabled:
            return httpx.Response(
                404, json={"code": "PGRST202", "message": f"Could not find the function public.{name}"}
            )
        profile = next((p for p in self.tables["profiles"] if p["id"] == args.get("p_user_id")), None)

        if name == "credit_balance":
            if not profile:
                return httpx.Response(200, json={"ok": False, "reason": "no_profile"})
            profile["balance_mt"] = float(profile.get("balance_mt", 0)) + float(args["p_amount"])
            return httpx.Response(200, json={"ok": True, "balance": profile["balance_mt"]})

        if name == "debit_balance":
            if not profile:
                return httpx.Response(200, json={"ok": False, "reason": "no_profile"})
            balance = float(profile.get("balance_mt", 0))
            amount = float(args["p_amount"])
            if balance < amount:
                return httpx.Response(200, json={"ok": False, "reason": "insufficient_funds", "balance": balance})
            profile["balance_mt"] = balance - amount
            return httpx.Response(200, json={"ok": True, "balance": profile["balance_mt"]})

        if name == "request_payout":
            if not profile:
                return httpx.Response(200, json={"ok": False, "reason": "no_profile"})
            balance = float(profile.get("balance_mt", 0))
            amount = float(args["p_amount"])
            if balance < amount:
                return httpx.Response(200, json={"ok": False, "reason": "insufficient_funds", "balance": balance})
            profile["balance_mt"] = balance - amount
            payout_id = uuid.uuid4().hex
            self.tables["payouts"].append(
                {
                    "id": payout_id,
                    "user_id": profile["id"],
                    "amount_mt": args["p_amount"],
                    "method": args["p_method"],
                    "phone": args["p_phone"],
                    "holder_name": args.get("p_holder", ""),
                    "status": "pending",
                    "admin_note": "",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
            return httpx.Response(200, json={"ok": True, "payout_id": payout_id, "balance": balance - amount})

        if name == "settle_payout":
            payout = next((p for p in self.tables["payouts"] if p["id"] == args["p_payout_id"]), None)
            if not payout:
                return httpx.Response(200, json={"ok": False, "reason": "not_found"})
            if payout["status"] != "pending":
                return httpx.Response(200, json={"ok": False, "reason": "already_decided"})
            payout["status"] = args["p_status"]
            payout["admin_note"] = args.get("p_note", "")
            payout["decided_at"] = "2026-01-02T00:00:00+00:00"
            if args["p_status"] == "rejected":
                owner = next(p for p in self.tables["profiles"] if p["id"] == payout["user_id"])
                owner["balance_mt"] = float(owner.get("balance_mt", 0)) + float(payout["amount_mt"])
            return httpx.Response(200, json={"ok": True, "status": args["p_status"]})

        return httpx.Response(404, json={"code": "PGRST202", "message": f"Could not find the function public.{name}"})


# --------------------------------------------------------------------------- #
# NOWPayments falsa
# --------------------------------------------------------------------------- #
class FakeNOWPayments:
    def __init__(self, *, status: str = "waiting", fail_create: bool = False) -> None:
        self.created: list[dict] = []
        self.status = status
        self.fail_create = fail_create
        self.fetched: list[str] = []

    def client(self) -> Any:
        from app.nowpayments import NOWPaymentsClient

        return NOWPaymentsClient(
            "np_test_key", base_url="https://api.nowpayments.io",
            transport=httpx.MockTransport(self.handle),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/status":
            return httpx.Response(200, json={"message": "API is running"})
        if path == "/v1/payment" and request.method == "POST":
            if self.fail_create:
                return httpx.Response(503, json={"message": "service unavailable"})
            payload = json.loads(request.content or b"{}")
            self.created.append(payload)
            return httpx.Response(
                200,
                json={
                    "payment_id": "9000001",
                    "payment_status": "waiting",
                    "pay_address": "TQn9Y2khDD95J42FQtQTdwBMRk1XfK6Z9p",
                    "price_amount": payload.get("price_amount"),
                    "price_currency": "usd",
                    "pay_amount": payload.get("price_amount"),
                    "pay_currency": payload.get("pay_currency"),
                    "order_id": payload.get("order_id"),
                    "payment_url": f"https://nowpayments.io/payment/{payload.get('order_id')}",
                },
            )
        if path.startswith("/v1/payment/"):
            payment_id = path.rsplit("/", 1)[1]
            self.fetched.append(payment_id)
            last = self.created[-1] if self.created else {}
            return httpx.Response(
                200,
                json={
                    "payment_id": payment_id,
                    "payment_status": self.status,
                    "actually_paid": last.get("price_amount", 10),
                    "outcome_amount": last.get("price_amount", 10),
                    "outcome_currency": "usdttrc20",
                    "order_id": last.get("order_id"),
                    "pay_address": "TQn9Y2khDD95J42FQtQTdwBMRk1XfK6Z9p",
                },
            )
        return httpx.Response(404, json={"message": f"sem rota {path}"})


def parse_query(url: str) -> dict:
    return dict(parse_qsl(url.split("?", 1)[-1]))


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def handler_for(routes: dict[str, Callable[[httpx.Request], httpx.Response]]):
    def handle(request: httpx.Request) -> httpx.Response:
        for prefix, callback in routes.items():
            if request.url.path.startswith(prefix):
                return callback(request)
        return httpx.Response(404, json={"message": "sem rota"})

    return httpx.MockTransport(handle)
