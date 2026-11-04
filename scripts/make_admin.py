"""Promove um utilizador a administrador.

Uso:
    python scripts/make_admin.py email@exemplo.com
    python scripts/make_admin.py --list

Usa a SUPABASE_SECRET_KEY (backend). Nao imprime chaves.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_settings  # noqa: E402
from app.db import SupabaseClient, SupabaseError  # noqa: E402


def main(argv: list[str]) -> int:
    try:
        settings = load_settings(strict=True)
    except ConfigError as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1

    db = SupabaseClient(
        settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY.get_secret_value()
    )

    if not argv or argv[0] in {"--list", "-l"}:
        try:
            rows = db.table("profiles").select("email,full_name,role,balance_mt").limit(200).execute()
        except SupabaseError as exc:
            print(f"[ERRO] {exc}", file=sys.stderr)
            return 1
        if not rows:
            print("Ainda nao ha perfis. Regista-te primeiro na aplicacao.")
            return 0
        for row in rows:
            print(f"  {row.get('role','user'):<6} {row.get('email','')}  ({row.get('balance_mt')} MT)")
        return 0

    email = argv[0].strip().lower()
    try:
        updated = db.update_where("profiles", {"role": "admin"}, email=email)
    except SupabaseError as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        print("Dica: corre primeiro supabase/schema.sql no SQL Editor da Supabase.", file=sys.stderr)
        return 1

    if not updated:
        print(f"Nenhum perfil com o email {email}. Regista a conta na app primeiro.")
        return 1
    print(f"OK: {email} agora e administrador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
