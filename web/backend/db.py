"""Camada de persistência do VFence Central.

O Central usa SQLite em desenvolvimento e PostgreSQL quando ``DATABASE_URL``
aponta para ``postgresql://`` ou ``postgres://``. Serviços e routers continuam
usando o mesmo subconjunto DB-API: ``execute()``, ``executemany()``,
``fetchone()``, ``fetchall()`` e ``rowcount``.

As diferenças de dialeto ficam concentradas neste módulo:

* SQLite usa placeholders ``?``; PostgreSQL usa ``%s``;
* cada backend possui seu schema idempotente;
* SQLite recebe PRAGMAs; PostgreSQL não;
* a geração da versão da cerca usa trava transacional do PostgreSQL.

Uma conexão é aberta por operação. Isso funciona em processos locais e em
instâncias serverless que podem nascer e morrer a qualquer momento. Em Neon ou
Supabase deve ser usada, preferencialmente, a URL de conexão com pool fornecida
pelo provedor.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.config import Settings
from backend.security import hash_token

logger = logging.getLogger("db")

BACKEND_DIR = Path(__file__).resolve().parent
SQLITE_SCHEMA_FILE = BACKEND_DIR / "schema.sql"
POSTGRESQL_SCHEMA_FILE = BACKEND_DIR / "schema_postgresql.sql"
FENCE_VERSION_LOCK_ID = 0x5646454E  # "VFEN", estável entre processos.

# Colunas de monitoramento acrescentadas depois da primeira versão do schema.
MONITORING_COLUMNS = ("gnss_status", "last_contact_at", "last_position_at", "link_status")
_TABLE_NAME = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE)
_INDEX_NAME = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE)


class DatabaseError(RuntimeError):
    """Erro público e sanitizado da camada de banco."""


class DatabaseConfigurationError(DatabaseError):
    pass


class DatabaseConnectionError(DatabaseError):
    pass


def _postgres_sql(sql: str) -> str:
    """Converte placeholders qmark do código existente para psycopg."""
    return sql.replace("?", "%s")


def _schema_statements(script: str) -> Iterator[str]:
    """Divide nossos schemas simples sem enviar comentários ao PostgreSQL."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("--")]
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            yield statement.strip()


def _database_host(url: str) -> str:
    """Só o servidor da URL: diz qual provedor é sem expor credenciais."""
    try:
        return urlsplit(url).hostname or "-"
    except ValueError:
        return "-"


def describe_connection_error(error: BaseException, url: str) -> str:
    """Resume a falha do driver sem a URL e sem a senha.

    A mensagem do libpq é o que diz POR QUE a conexão falhou (senha
    recusada, servidor inexistente, projeto suspenso, tempo esgotado). Sem
    ela, o log do Vercel mostrava só "database connection failed" e não
    havia como saber se o problema era a variável ou o provedor.
    """
    text = " ".join(str(error).split())
    try:
        password = urlsplit(url).password
    except ValueError:
        password = None
    # Só segredos não vazios: `str.replace("", ...)` poria "***" entre
    # todas as letras da mensagem (SQLite, por exemplo, não tem URL).
    for secret in (url, password):
        if secret:
            text = text.replace(secret, "***")
    return text[:300] or type(error).__name__


class _PostgresConnection:
    """Adaptador mínimo para manter SQL e serviços independentes do driver."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Iterable[Any] | None = None):
        return self._connection.execute(
            _postgres_sql(sql), tuple(parameters) if parameters is not None else None
        )

    def executemany(self, sql: str, parameters: Iterable[Iterable[Any]]):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(sql), parameters)
        return cursor


class Database:
    """Banco do Central, selecionado por caminho SQLite ou URL PostgreSQL."""

    def __init__(self, target: Path | str) -> None:
        self._lock = threading.RLock()
        self._state = threading.local()
        self.path: Path | None = None
        self.database_url: str | None = None

        if isinstance(target, Path):
            self.backend = "sqlite"
            self.path = target
            return

        value = str(target)
        if value.startswith(("postgresql://", "postgres://")):
            self.backend = "postgresql"
            self.database_url = value
        elif value.startswith("sqlite:///"):
            self.backend = "sqlite"
            raw_path = value[len("sqlite:///"):]
            self.path = Path(raw_path)
        elif "://" in value:
            raise DatabaseConfigurationError("unsupported database scheme")
        else:
            # Compatibilidade com os testes e chamadas antigas Database(path).
            self.backend = "sqlite"
            self.path = Path(value)

    @property
    def is_postgresql(self) -> bool:
        return self.backend == "postgresql"

    def _open_postgresql(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise DatabaseConfigurationError(
                "driver PostgreSQL não instalado; instale as dependências do Central"
            ) from None

        try:
            return psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=10,
                prepare_threshold=None,
            )
        except psycopg.Error as error:
            # A URL pode conter usuário e senha; nunca propagamos seu conteúdo.
            # A exceção pública continua genérica; o log leva o motivo, já
            # sem a URL e sem a senha, e o servidor (que identifica o provedor).
            logger.error(
                "database connection failed (host=%s, error=%s, sqlstate=%s): %s",
                _database_host(self.database_url or ""),
                type(error).__name__,
                getattr(error, "sqlstate", None) or "-",
                describe_connection_error(error, self.database_url or ""),
            )
            raise DatabaseConnectionError("database connection failed") from None

    @contextmanager
    def connect(self):
        """Abre uma transação, confirma no sucesso e desfaz no erro.

        Chamadas aninhadas na mesma thread reutilizam a transação externa. Isso
        permite que um lote inteiro seja atômico mesmo quando seus serviços
        auxiliares também usam ``with db.connect()``.
        """
        current = getattr(self._state, "connection", None)
        if current is not None:
            yield current
            return

        lock = nullcontext() if self.is_postgresql else self._lock
        with lock:
            if self.is_postgresql:
                raw_connection = self._open_postgresql()
                connection = _PostgresConnection(raw_connection)
            else:
                assert self.path is not None
                if self.path != Path(":memory:"):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                raw_connection = sqlite3.connect(self.path, timeout=10)
                raw_connection.row_factory = sqlite3.Row
                raw_connection.execute("PRAGMA foreign_keys = ON")
                connection = raw_connection

            self._state.connection = connection
            try:
                yield connection
                raw_connection.commit()
            except Exception:
                raw_connection.rollback()
                raise
            finally:
                del self._state.connection
                raw_connection.close()

    def initialize(self) -> None:
        """Cria o schema correto. Pode ser chamada em toda inicialização."""
        if self.is_postgresql:
            script = POSTGRESQL_SCHEMA_FILE.read_text(encoding="utf-8")
            with self.connect() as conn:
                if self._postgres_schema_ready(conn, script):
                    logger.info("database ready (backend=postgresql, schema up to date)")
                    return
                # Só chega aqui no primeiro deploy ou depois de mudança no
                # schema. O limite de espera impede que uma subida fique
                # presa atrás de um lote longo da Base: se estourar, a
                # inicialização falha, a aplicação continua no ar e tenta
                # de novo na próxima requisição (ver main.py).
                conn.execute("SET LOCAL lock_timeout = '10s'")
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('vfence_schema_init'))")
                for statement in _schema_statements(script):
                    conn.execute(statement)
                self._migrate_monitoring(conn)
        else:
            script = SQLITE_SCHEMA_FILE.read_text(encoding="utf-8")
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(script)
                self._migrate_monitoring(conn)
                conn.execute("PRAGMA optimize")
        logger.info("database ready (backend=%s)", self.backend)

    def _postgres_schema_ready(self, conn, script: str) -> bool:
        """Confere, só lendo o catálogo, se tabelas, índices e colunas já existem.

        `CREATE ... IF NOT EXISTS` parece inofensivo, mas `CREATE INDEX`
        trava a tabela em modo SHARE e `ALTER TABLE` em ACCESS EXCLUSIVE
        mesmo quando nada muda. No Vercel isso roda a cada subida a frio e
        disputava `telemetry` e `collars` com o lote da Base, que trava as
        duas na ordem inversa — receita de deadlock. Ler o catálogo não
        trava tabela nenhuma.
        """
        tables = {row["name"] for row in conn.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )}
        indexes = {row["name"] for row in conn.execute(
            "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()"
        )}
        return (
            set(_TABLE_NAME.findall(script)) <= tables
            and set(_INDEX_NAME.findall(script)) <= indexes
            and set(MONITORING_COLUMNS) <= self._collar_columns(conn)
        )

    def _collar_columns(self, conn) -> set[str]:
        if self.is_postgresql:
            rows = conn.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'collars'"
            )
        else:
            rows = conn.execute("PRAGMA table_info(collars)")
        return {row["name"] for row in rows}

    def _migrate_monitoring(self, conn) -> None:
        """Acrescenta as colunas de monitoramento que faltarem.

        O preenchimento a partir do histórico só roda quando a coluna acabou
        de nascer: depois disso, toda gravação de telemetria já mantém as
        duas colunas, e repetir os UPDATEs a cada subida só travaria linhas.
        """
        added = [c for c in MONITORING_COLUMNS if c not in self._collar_columns(conn)]
        for column in added:
            conn.execute(f"ALTER TABLE collars ADD COLUMN {column} TEXT")
        if "last_position_at" in added:
            conn.execute(
                """UPDATE collars SET last_position_at=(
                     SELECT ts FROM telemetry WHERE telemetry.collar_id=collars.collar_id
                     AND lat IS NOT NULL AND lon IS NOT NULL AND COALESCE(zone,'')<>'GNSS_INVALIDO'
                     ORDER BY id DESC LIMIT 1
                   ) WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL"""
            )
        if "last_contact_at" in added:
            conn.execute("UPDATE collars SET last_contact_at=last_seen")

    def next_fence_version(self, conn) -> int:
        """Reserva com segurança a próxima versão dentro da transação atual."""
        if self.is_postgresql:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (FENCE_VERSION_LOCK_ID,))
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS maximum FROM fences"
        ).fetchone()
        return int(row["maximum"]) + 1

    def bind_property(self, property_id: str, base_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO property_binding (singleton,property_id,base_id) VALUES (1,?,?) "
                "ON CONFLICT(singleton) DO NOTHING", (property_id, base_id),
            )
            binding = conn.execute("SELECT property_id,base_id FROM property_binding WHERE singleton=1").fetchone()
            if (binding["property_id"], binding["base_id"]) != (property_id, base_id):
                raise DatabaseConfigurationError("property storage already has a different owner")
            if conn.execute("SELECT 1 FROM bases WHERE base_id<>? LIMIT 1", (base_id,)).fetchone():
                raise DatabaseConfigurationError("storage has other bases; split or restore before binding")

    def seed_from_settings(self, settings: Settings) -> None:
        """Cadastra/atualiza a Base e cadastra coleiras sem apagar estado."""
        token_hash = hash_token(settings.base_token)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bases (base_id, token_hash, lat, lon)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(base_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    lat        = excluded.lat,
                    lon        = excluded.lon
                """,
                (settings.base_id, token_hash, settings.base_lat, settings.base_lon),
            )
            for collar_id in settings.known_collars:
                conn.execute(
                    """
                    INSERT INTO collars (collar_id, base_id, last_seen)
                    VALUES (?, ?, NULL)
                    ON CONFLICT(collar_id) DO NOTHING
                    """,
                    (collar_id, settings.base_id),
                )

        logger.info(
            "base=%s registered (lat=%.6f, lon=%.6f, collars=%s)",
            settings.base_id,
            settings.base_lat,
            settings.base_lon,
            ",".join(settings.known_collars) or "-",
        )

    def table_names(self) -> list[str]:
        with self.connect() as conn:
            if self.is_postgresql:
                rows = conn.execute(
                    """SELECT table_name AS name FROM information_schema.tables
                       WHERE table_schema = current_schema() ORDER BY table_name"""
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall()
        return [row["name"] for row in rows]

    def get_base(self, base_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bases WHERE base_id = ?", (base_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_collars(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM collars ORDER BY collar_id").fetchall()
        return [dict(row) for row in rows]
