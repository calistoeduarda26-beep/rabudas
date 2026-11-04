"""Aplica `supabase/schema.sql` na base de dados Supabase.

Uso:
    pip install "psycopg[binary]"        # uma vez (nao esta no requirements.txt
                                         # para nao pesar no bundle da Vercel)
    python scripts/init_db.py            # usa POSTGRES_URL do .env
    python scripts/init_db.py --check    # so lista as tabelas existentes

Alternativa sem instalar nada: cola o conteudo de supabase/schema.sql no
SQL Editor da Supabase e corre.

Nao imprime chaves nem passwords.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA = ROOT / "supabase" / "schema.sql"

EXPECTED_TABLES = ("profiles", "payments", "payouts", "login_logs", "settings")


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _connection_string() -> str:
    _load_dotenv()
    url = os.environ.get("POSTGRES_URL") or os.environ.get("POSTGRES_URL_NON_POOLING") or ""
    if not url:
        print(
            "[ERRO] POSTGRES_URL nao definido no .env.\n"
            "       Copia-o de: Supabase -> Project Settings -> Database -> Connection string\n"
            "       (usa a porta 5432, nao a 6543 do pgbouncer).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return url


def _connect(url: str):  # noqa: ANN202
    try:
        import psycopg  # type: ignore

        return psycopg.connect(url, autocommit=True), "psycopg3"
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2.connect(url), "psycopg2"
    except ImportError:
        print(
            '[ERRO] Falta um driver PostgreSQL. Instala com:\n'
            '           pip install "psycopg[binary]"\n'
            "       Ou cola supabase/schema.sql no SQL Editor da Supabase.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _list_tables(conn) -> list[str]:  # noqa: ANN001
    with conn.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by table_name"
        )
        return [row[0] for row in cur.fetchall()]


def main(argv: list[str]) -> int:
    url = _connection_string()
    conn, driver = _connect(url)
    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"Ligado com {driver} a {host}")

    if "--check" in argv:
        existentes = _list_tables(conn)
        print("Tabelas em public:", existentes or "(nenhuma)")
        em_falta = [t for t in EXPECTED_TABLES if t not in existentes]
        if em_falta:
            print(f"[FALTA] {', '.join(em_falta)} -> corre: python scripts/init_db.py")
            return 1
        print("OK: todas as tabelas necessarias existem.")
        return 0

    sql = SCHEMA.read_text(encoding="utf-8")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    except Exception as exc:
        print(f"[ERRO] Falha ao aplicar o schema: {exc}", file=sys.stderr)
        return 1
    if hasattr(conn, "commit"):
        conn.commit()

    existentes = _list_tables(conn)
    print("Schema aplicado. Tabelas em public:", existentes)
    em_falta = [t for t in EXPECTED_TABLES if t not in existentes]
    if em_falta:
        print(f"[AVISO] continuam em falta: {', '.join(em_falta)}", file=sys.stderr)
        return 1
    print("OK. Agora: python scripts/make_admin.py o-teu-email")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
