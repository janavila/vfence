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
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.security import hash_token

logger = logging.getLogger("db")

BACKEND_DIR = Path(__file__).resolve().parent
SQLITE_SCHEMA_FILE = BACKEND_DIR / "schema.sql"
POSTGRESQL_SCHEMA_FILE = BACKEND_DIR / "schema_postgresql.sql"
FENCE_VERSION_LOCK_ID = 0x5646454E  # "VFEN", estável entre processos.


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
        except psycopg.Error:
            # A URL pode conter usuário e senha; nunca propagamos seu conteúdo.
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
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('vfence_schema_init'))")
                for statement in _schema_statements(script):
                    conn.execute(statement)
        else:
            script = SQLITE_SCHEMA_FILE.read_text(encoding="utf-8")
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(script)
                conn.execute("PRAGMA optimize")
        logger.info("database ready (backend=%s)", self.backend)

    def next_fence_version(self, conn) -> int:
        """Reserva com segurança a próxima versão dentro da transação atual."""
        if self.is_postgresql:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (FENCE_VERSION_LOCK_ID,))
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS maximum FROM fences"
        ).fetchone()
        return int(row["maximum"]) + 1

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
