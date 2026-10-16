"""Testes rápidos da seleção e adaptação SQLite/PostgreSQL.

Não abrem conexão externa. Os testes que usam um PostgreSQL real ficam em
``test_postgresql_integration.py`` e só rodam com ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import ConfigError, load_settings
from backend.db import (
    Database,
    DatabaseConfigurationError,
    DatabaseConnectionError,
    _postgres_sql,
    _schema_statements,
)
from backend.services import edge_service


def test_sem_database_url_seleciona_sqlite(tmp_path: Path):
    db = Database(tmp_path / "central.db")

    assert db.backend == "sqlite"
    assert db.path == tmp_path / "central.db"
    assert db.database_url is None


def test_sqlite_url_continua_disponivel_para_desenvolvimento(tmp_path: Path):
    db = Database(f"sqlite:///{tmp_path / 'central.db'}")

    assert db.backend == "sqlite"
    db.initialize()
    assert "fences" in db.table_names()


def test_postgresql_url_seleciona_backend_sem_expor_url():
    url = "postgresql://usuario:senha@host.exemplo/vfence?sslmode=require"
    db = Database(url)

    assert db.backend == "postgresql"
    assert db.is_postgresql is True
    assert db.path is None


def test_esquema_desconhecido_e_recusado_sem_credenciais():
    secret_url = "mysql://usuario:senha-secreta@host/vfence"

    with pytest.raises(DatabaseConfigurationError) as error:
        Database(secret_url)

    assert "senha-secreta" not in str(error.value)


def test_falha_do_driver_postgresql_e_sanitizada(monkeypatch):
    import psycopg

    secret_url = "postgresql://usuario:senha-secreta@host/vfence"

    def fail(*_args, **_kwargs):
        raise psycopg.OperationalError(f"não conectou em {secret_url}")

    monkeypatch.setattr(psycopg, "connect", fail)
    db = Database(secret_url)

    with pytest.raises(DatabaseConnectionError) as error:
        with db.connect():
            pass

    assert str(error.value) == "database connection failed"
    assert "senha-secreta" not in str(error.value)


def test_database_url_invalida_gera_erro_tratavel_sem_vazar_valor(
    monkeypatch: pytest.MonkeyPatch,
):
    secret_url = "mysql://usuario:senha-secreta@host/vfence"
    monkeypatch.setenv("DATABASE_URL", secret_url)

    with pytest.raises(ConfigError) as error:
        load_settings(env_file=None)

    assert "DATABASE_URL" in str(error.value)
    assert secret_url not in str(error.value)
    assert "senha-secreta" not in str(error.value)


def test_placeholders_sao_convertidos_so_na_fronteira_postgresql():
    sql = "SELECT * FROM fences WHERE version = ? AND status = ?"

    assert _postgres_sql(sql) == (
        "SELECT * FROM fences WHERE version = %s AND status = %s"
    )


def test_schema_postgresql_e_idempotente_e_sem_sql_especifico_do_sqlite():
    schema = (
        Path(__file__).parents[1] / "backend" / "schema_postgresql.sql"
    ).read_text(encoding="utf-8")
    statements = list(_schema_statements(schema))

    assert statements
    assert all("IF NOT EXISTS" in statement for statement in statements)
    assert "PRAGMA" not in schema
    assert "AUTOINCREMENT" not in schema
    assert "INSERT OR " not in schema


def test_batch_desfaz_claim_se_gravacao_falhar(database, settings, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(edge_service, "_store_telemetry", fail)
    item = {
        "edge_seq": 91,
        "type": "telemetry",
        "data": {"collar_id": "COL01"},
    }

    with pytest.raises(RuntimeError, match="falha simulada"):
        edge_service.process_batch(database, settings, settings.base_id, [item])

    with database.connect() as conn:
        claim = conn.execute(
            "SELECT 1 FROM edge_items WHERE base_id = ? AND edge_seq = ?",
            (settings.base_id, 91),
        ).fetchone()
    assert claim is None
