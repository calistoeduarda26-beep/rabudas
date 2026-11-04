"""Testes da camada de configuracao e da proteccao das chaves."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from app.config import (
    ENV_FILE,
    OPTIONAL_WARN_VARS,
    REQUIRED_VARS,
    ConfigError,
    Settings,
    load_settings,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent

FULL_ENV = [
    "NOWPAYMENTS_API_KEY=abc123",
    "NOWPAYMENTS_PUBLIC_KEY=pub-123",
    "NOWPAYMENTS_IPN_SECRET=ipn-123",
    "SUPABASE_URL=https://x.supabase.co/",
    "SUPABASE_PUBLISHABLE_KEY=sb_publishable_x",
    "SUPABASE_SECRET_KEY=sb_secret_x",
    f"APP_SECRET_KEY={'a' * 64}",
    "BASE_URL=http://localhost:8000",
    "RATE_MT_PER_USDT=80",
]


def write_env(tmp_path: pathlib.Path, lines: list[str]) -> pathlib.Path:
    target = tmp_path / ".env"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Ficheiros no repositorio
# --------------------------------------------------------------------------- #
def test_gitignore_bloqueia_env():
    linhas = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in linhas
    assert "!.env.example" in linhas


def test_env_nao_esta_versionado_no_git():
    resultado = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert resultado.returncode != 0, ".env nao pode estar versionado"


def test_git_check_ignore_confirma_env():
    resultado = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True
    )
    assert resultado.returncode == 0
    assert resultado.stdout.strip() == ".env"


def test_env_example_tem_todas_as_variaveis_e_nenhum_valor():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    nomes = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    assert set(REQUIRED_VARS) <= nomes
    for name in OPTIONAL_WARN_VARS:
        assert name in nomes
    for line in example.splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        assert value.strip() == "", f"{line!r} nao devia ter valor"


def test_env_real_existe_com_valores():
    if not ENV_FILE.exists():
        pytest.skip(".env nao esta no git (gitignored); corre 'cp .env.example .env'")
    settings = load_settings(strict=True, env_file=str(ENV_FILE))
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value()
    assert settings.SUPABASE_SECRET_KEY.get_secret_value().startswith("sb_secret_")
    assert settings.SUPABASE_PUBLISHABLE_KEY.startswith("sb_publishable_")
    assert len(settings.APP_SECRET_KEY.get_secret_value()) >= 64
    assert float(settings.RATE_MT_PER_USDT) == 80
    assert settings.SUPABASE_URL == "https://tegowntlzozemmmzvesm.supabase.co"


# --------------------------------------------------------------------------- #
# Validacao
# --------------------------------------------------------------------------- #
def test_placeholder_e_tratado_como_falta(tmp_path):
    linhas = [
        line if not line.startswith("APP_SECRET_KEY") and not line.startswith("NOWPAYMENTS_IPN_SECRET")
        else line.split("=", 1)[0] + "=<gerar no painel>"
        for line in FULL_ENV
    ]
    env = write_env(tmp_path, linhas)
    with pytest.raises(ConfigError) as erro:
        load_settings(strict=True, env_file=str(env))
    mensagem = str(erro.value)
    assert "APP_SECRET_KEY" in mensagem
    assert "NOWPAYMENTS_IPN_SECRET" not in mensagem  # e opcional
    assert "sb_secret_x" not in mensagem  # nunca mostra valores


def test_variavel_em_falta_e_apontada_por_nome(tmp_path):
    env = write_env(tmp_path, [line for line in FULL_ENV if not line.startswith("SUPABASE_SECRET_KEY")])
    with pytest.raises(ConfigError) as erro:
        load_settings(strict=True, env_file=str(env))
    assert "SUPABASE_SECRET_KEY" in str(erro.value)


def test_app_secret_curta_e_recusada(tmp_path):
    linhas = [line if not line.startswith("APP_SECRET_KEY") else "APP_SECRET_KEY=curta" for line in FULL_ENV]
    env = write_env(tmp_path, linhas)
    with pytest.raises(ConfigError):
        load_settings(strict=True, env_file=str(env))


def test_url_fica_sem_barra_final(tmp_path):
    env = write_env(tmp_path, FULL_ENV)
    settings = load_settings(strict=True, env_file=str(env))
    assert settings.SUPABASE_URL == "https://x.supabase.co"
    assert settings.supabase_rest_url == "https://x.supabase.co/rest/v1"
    assert settings.supabase_auth_url == "https://x.supabase.co/auth/v1"


def test_load_sem_strict_nao_falha(tmp_path):
    env = write_env(tmp_path, ["SUPABASE_URL=https://x.supabase.co"])
    settings = load_settings(strict=False, env_file=str(env))
    assert "SUPABASE_SECRET_KEY" in settings.missing_required()


# --------------------------------------------------------------------------- #
# Nao vazar chaves
# --------------------------------------------------------------------------- #
def test_secretstr_nao_aparece_no_repr(tmp_path):
    env = write_env(tmp_path, FULL_ENV)
    settings = load_settings(strict=True, env_file=str(env))
    representacao = repr(settings)
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value() not in representacao
    assert settings.SUPABASE_SECRET_KEY.get_secret_value() not in representacao
    assert "**********" in representacao


def test_config_publica_so_tem_o_que_e_publico(tmp_path):
    env = write_env(tmp_path, FULL_ENV)
    settings = load_settings(strict=True, env_file=str(env))
    publica = json.dumps(settings.public_client_config())
    assert settings.SUPABASE_SECRET_KEY.get_secret_value() not in publica
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value() not in publica
    assert settings.APP_SECRET_KEY.get_secret_value() not in publica
    assert settings.SUPABASE_PUBLISHABLE_KEY in publica


def test_resumo_mascarado_nao_mostra_chaves_completas(tmp_path):
    env = write_env(tmp_path, FULL_ENV)
    settings = load_settings(strict=True, env_file=str(env))
    resumo = json.dumps(settings.masked_summary(), default=str, ensure_ascii=False)
    assert settings.NOWPAYMENTS_API_KEY.get_secret_value() not in resumo
    assert settings.SUPABASE_SECRET_KEY.get_secret_value() not in resumo
    assert "abc1…23" in resumo


def test_ipn_activa_so_com_segredo():
    assert Settings(NOWPAYMENTS_IPN_SECRET="").ipn_enabled is False
    assert Settings(NOWPAYMENTS_IPN_SECRET="segredo").ipn_enabled is True
