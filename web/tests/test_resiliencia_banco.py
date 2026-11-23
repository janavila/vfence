"""Central com o banco fora do ar: diagnosticar sem cair.

Estes testes nasceram de uma queda real no Vercel (novembro de 2026): o
PostgreSQL parou de aceitar conexões e, como a preparação do banco rodava
no `lifespan` e levantava a exceção, o Uvicorn encerrava o processo. Toda
rota — inclusive a tela de login — virava `500 FUNCTION_INVOCATION_FAILED`,
e o log dizia só "database connection failed", sem o motivo.

O que fica garantido aqui:

1. o motivo da falha de conexão vai para o log, sem URL e sem senha;
2. com o banco indisponível as telas abrem e a API responde 503;
3. quando o banco volta, a API volta sozinha, sem reiniciar o servidor;
4. erro de CONFIGURAÇÃO na subida continua derrubando, de propósito;
5. falta de disco para o arquivo de log não derruba;
6. com o schema PostgreSQL já completo, a subida não roda DDL nenhum
   (era o DDL que travava tabelas e podia entrar em deadlock com a Base).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import (
    MONITORING_COLUMNS,
    POSTGRESQL_SCHEMA_FILE,
    SQLITE_SCHEMA_FILE,
    Database,
    DatabaseConfigurationError,
    DatabaseConnectionError,
    _INDEX_NAME,
    _TABLE_NAME,
    describe_connection_error,
)
from backend.logging_setup import setup_logging
from backend.main import create_app
from backend.security import SESSION_COOKIE, create_session_token


# ---------------------------------------------------------------------------
# 1. Motivo da falha no log, sem segredo
# ---------------------------------------------------------------------------


def test_falha_de_conexao_registra_motivo_sem_url_nem_senha(monkeypatch, caplog):
    import psycopg

    secret_url = "postgresql://dono:senha-secreta@ep-exemplo.neon.tech/vfence?sslmode=require"

    def fail(*_args, **_kwargs):
        raise psycopg.OperationalError(
            f'connection to {secret_url} failed: FATAL: password authentication '
            'failed for user "dono" (senha-secreta)'
        )

    monkeypatch.setattr(psycopg, "connect", fail)
    db = Database(secret_url)

    with caplog.at_level(logging.ERROR, logger="db"):
        with pytest.raises(DatabaseConnectionError) as error:
            with db.connect():
                pass

    # A exceção pública continua genérica (contrato do teste mais antigo).
    assert str(error.value) == "database connection failed"
    registro = caplog.text
    assert "ep-exemplo.neon.tech" in registro            # qual provedor
    assert "password authentication failed" in registro  # por quê
    assert "OperationalError" in registro
    assert "senha-secreta" not in registro
    assert secret_url not in registro


def test_resumo_do_erro_sem_url_preserva_a_mensagem():
    """Sem DATABASE_URL (SQLite) não há o que esconder: a mensagem fica intacta."""
    erro = PermissionError(13, "Permission denied", "/var/task/data")

    assert describe_connection_error(erro, "") == (
        "[Errno 13] Permission denied: '/var/task/data'"
    )


# ---------------------------------------------------------------------------
# 2 e 3. Banco indisponível na subida: telas no ar, API 503, volta sozinha
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_sem_banco(settings, tmp_path: Path):
    """Configuração cujo SQLite não pode ser criado.

    `DATA_DIR` aponta para um ARQUIVO comum: criar `vfence.db` "dentro"
    dele falha com OSError — o mesmo efeito do disco somente-leitura do
    Vercel, sem depender de permissões do sistema.
    """
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("não sou uma pasta", encoding="utf-8")
    return replace(settings, data_dir=bloqueio)


def _logar(client: TestClient, settings) -> None:
    token = create_session_token(settings.admin_user, settings.session_secret, 3600)
    client.cookies.set(SESSION_COOKIE, token)


def test_banco_indisponivel_na_subida_nao_derruba_as_telas(settings_sem_banco):
    app = create_app(settings=settings_sem_banco, log_to_file=False)

    with TestClient(app) as client:  # antes: a subida levantava exceção aqui
        assert app.state.db_ready is False
        assert client.get("/login").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"

        # Sem login, continua 401: o banco nem é consultado.
        assert client.get("/api/fences/active").status_code == 401

        _logar(client, settings_sem_banco)
        assert client.get("/editor").status_code == 200
        resposta = client.get("/api/fences/active")
        assert resposta.status_code == 503
        assert resposta.json() == {"detail": "database_unavailable"}
        assert resposta.headers["Retry-After"] == "5"


def test_base_recebe_503_e_nao_500_com_banco_indisponivel(settings_sem_banco):
    """O Raspberry trata 503 como falha temporária e tenta de novo."""
    app = create_app(settings=settings_sem_banco, log_to_file=False)
    headers = {"Authorization": f"Bearer {settings_sem_banco.base_token}"}

    with TestClient(app) as client:
        resposta = client.post("/api/edge/heartbeat", json={}, headers=headers)

    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "database_unavailable"}


def test_api_volta_sozinha_quando_o_banco_volta(settings_sem_banco):
    app = create_app(settings=settings_sem_banco, log_to_file=False)

    with TestClient(app) as client:
        _logar(client, settings_sem_banco)
        assert client.get("/api/fences/active").status_code == 503

        # O "banco" volta: o arquivo que bloqueava vira a pasta de dados.
        bloqueio = settings_sem_banco.data_dir
        bloqueio.unlink()
        bloqueio.mkdir()
        app.state.db_retry_s = 0  # sem esperar os 5 s de intervalo

        resposta = client.get("/api/fences/active")
        assert resposta.status_code == 404  # banco pronto, só não há cerca ainda
        assert app.state.db_ready is True
        # A Base foi cadastrada na reconexão, como seria na subida.
        assert Database(settings_sem_banco.database_target).get_base("BASE01") is not None


def test_tentativas_respeitam_o_intervalo(settings_sem_banco, monkeypatch):
    """Banco fora do ar não pode receber uma conexão por requisição."""
    app = create_app(settings=settings_sem_banco, log_to_file=False)
    tentativas = []
    original = Database.initialize

    def contar(self):
        tentativas.append(1)
        return original(self)

    monkeypatch.setattr(Database, "initialize", contar)

    with TestClient(app) as client:
        _logar(client, settings_sem_banco)
        for _ in range(5):
            assert client.get("/api/fences/active").status_code == 503

    assert len(tentativas) == 1  # só a da subida; as 5 caíram no intervalo


def test_erro_de_configuracao_na_subida_continua_fatal(settings, monkeypatch):
    def recusar(_self):
        raise DatabaseConfigurationError("property storage already has a different owner")

    monkeypatch.setattr(Database, "initialize", recusar)
    app = create_app(settings=settings, log_to_file=False)

    with pytest.raises(DatabaseConfigurationError):
        with TestClient(app):
            pass


# ---------------------------------------------------------------------------
# 5. Arquivo de log impossível de criar
# ---------------------------------------------------------------------------


def test_log_sem_disco_cai_para_o_console(tmp_path: Path, capsys):
    """`setup_logging` troca os handlers da raiz, então lemos o console."""
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("não sou uma pasta", encoding="utf-8")

    setup_logging("WARNING", bloqueio / "logs" / "central.log")  # não levanta

    assert "log file disabled, console only" in capsys.readouterr().out
    setup_logging("WARNING", None)


# ---------------------------------------------------------------------------
# 6. Subida a frio sem DDL quando o schema PostgreSQL já está completo
# ---------------------------------------------------------------------------


class _CatalogoFalso:
    """Conexão PostgreSQL de mentira: responde ao catálogo e grava o resto."""

    def __init__(self, *, sem_indice: str | None = None, sem_coluna: str | None = None):
        script = POSTGRESQL_SCHEMA_FILE.read_text(encoding="utf-8")
        self.tabelas = set(_TABLE_NAME.findall(script))
        self.indices = set(_INDEX_NAME.findall(script)) - {sem_indice}
        self.colunas = set(MONITORING_COLUMNS) - {sem_coluna}
        self.comandos: list[str] = []

    def execute(self, sql, _parametros=None):
        self.comandos.append(" ".join(sql.split()))
        if "information_schema.tables" in sql:
            return [{"name": nome} for nome in self.tabelas]
        if "pg_indexes" in sql:
            return [{"name": nome} for nome in self.indices]
        if "information_schema.columns" in sql:
            return [{"name": nome} for nome in self.colunas]
        return []


def _db_postgres_com(conexao: _CatalogoFalso, monkeypatch) -> Database:
    db = Database("postgresql://usuario:senha@host.exemplo/vfence")

    @contextmanager
    def connect():
        yield conexao

    monkeypatch.setattr(db, "connect", connect)
    return db


def test_schema_completo_nao_roda_ddl_nem_trava_tabelas(monkeypatch):
    conexao = _CatalogoFalso()
    _db_postgres_com(conexao, monkeypatch).initialize()

    escritas = [c for c in conexao.comandos if not c.startswith("SELECT")]
    assert escritas == []
    assert not any("pg_advisory" in c for c in conexao.comandos)


@pytest.mark.parametrize("falta", [{"sem_indice": "idx_telemetry_collar"},
                                   {"sem_coluna": "link_status"}])
def test_schema_incompleto_migra_com_trava_e_limite_de_espera(monkeypatch, falta):
    conexao = _CatalogoFalso(**falta)
    _db_postgres_com(conexao, monkeypatch).initialize()

    comandos = conexao.comandos
    assert "SET LOCAL lock_timeout = '10s'" in comandos
    trava = comandos.index("SELECT pg_advisory_xact_lock(hashtext('vfence_schema_init'))")
    assert any(c.startswith("CREATE TABLE IF NOT EXISTS fences") for c in comandos[trava:])


def test_nomes_do_schema_sao_lidos_do_proprio_arquivo():
    """A conferência acompanha o schema sem lista duplicada no código."""
    script = POSTGRESQL_SCHEMA_FILE.read_text(encoding="utf-8")

    assert {"fences", "collars", "telemetry", "edge_receipts", "property_binding"} <= set(
        _TABLE_NAME.findall(script)
    )
    assert "idx_fences_single_active" in _INDEX_NAME.findall(script)


# ---------------------------------------------------------------------------
# Migração das colunas de monitoramento num banco antigo
# ---------------------------------------------------------------------------


def test_banco_antigo_ganha_colunas_preenchidas_pelo_historico(tmp_path: Path):
    """O banco criado antes de 21/11 não tinha as colunas de monitoramento.

    O preenchimento agora roda só quando a coluna nasce — então ele
    precisa acertar de primeira.
    """
    arquivo = tmp_path / "antigo.db"
    with sqlite3.connect(arquivo) as conn:
        conn.executescript(SQLITE_SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO bases (base_id, token_hash) VALUES ('BASE01', 'x')")
        conn.execute(
            "INSERT INTO collars (collar_id, base_id, last_lat, last_lon, last_seen) "
            "VALUES ('COL01', 'BASE01', -31.3, -54.0, '2026-11-20T10:05:00Z')"
        )
        conn.executemany(
            "INSERT INTO telemetry (base_id, edge_seq, collar_id, ts, lat, lon, zone) "
            "VALUES ('BASE01', ?, 'COL01', ?, ?, ?, ?)",
            [
                (1, "2026-11-20T10:00:00Z", -31.3, -54.0, "SEGURA"),
                (2, "2026-11-20T10:05:00Z", None, None, "GNSS_INVALIDO"),
            ],
        )

    db = Database(arquivo)
    db.initialize()
    coleira = db.list_collars()[0]

    assert coleira["last_position_at"] == "2026-11-20T10:00:00Z"  # última posição válida
    assert coleira["last_contact_at"] == "2026-11-20T10:05:00Z"   # último contato
    db.initialize()  # segunda subida: nada a migrar, nada muda
    assert db.list_collars()[0] == coleira
