"""Acesso ao banco SQLite do Central.

O que faz
---------
Três coisas: abre conexões com o SQLite, cria as tabelas a partir do
`schema.sql` e faz o cadastro inicial da Base e das coleiras conhecidas
a partir do `.env`.

Por que SQLite "na mão", sem ORM
--------------------------------
É o padrão que o VFence Monitor já usa (`app/database.py`), e a
especificação pede continuidade. Para o tamanho deste projeto um ORM
seria mais coisa para aprender e manter do que ajuda: as consultas aqui
são simples e o SQL fica visível, o que é bom para o relatório.

O padrão herdado do Monitor, e o porquê de cada peça
----------------------------------------------------
- **`@contextmanager connect()`**: garante `commit` no sucesso,
  `rollback` no erro e `close` sempre. Ninguém precisa lembrar disso em
  cada consulta.
- **`threading.RLock`**: o FastAPI roda as consultas em threads
  separadas (via `asyncio.to_thread`). O SQLite aceita leituras
  simultâneas, mas uma escrita concorrente pode devolver "database is
  locked". A trava serializa as operações do nosso processo. É `RLock`
  (reentrante) e não `Lock` porque um método que já segura a trava pode
  chamar outro que também a pede, e com `Lock` isso travaria para sempre.
- **`row_factory = sqlite3.Row`**: as linhas viram objetos acessíveis
  por nome de coluna, e `dict(row)` entrega um dicionário que o FastAPI
  serializa direto em JSON.
- **`PRAGMA foreign_keys = ON`**: o SQLite ignora chaves estrangeiras
  por padrão. Sem este pragma, `fence_points.fence_id` apontando para
  uma cerca inexistente passaria sem reclamar.

Uma diferença em relação ao Monitor: aqui também ligamos
`PRAGMA journal_mode = WAL`, que permite ler enquanto se escreve. O
Central tem um WebSocket lendo o tempo todo enquanto a Base envia
lotes, e o WAL evita bloqueio mútuo nesse cenário.

Onde se conecta
---------------
`main.py` cria um `Database` por aplicação e o guarda em `app.state.db`.
As rotas das fases seguintes recebem esse objeto e chamam seus métodos
sempre dentro de `asyncio.to_thread`, para não travar o servidor.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.config import Settings
from backend.security import hash_token

logger = logging.getLogger("db")

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


class Database:
    """Conexões e operações de banco do Central."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Trava única por instância; ver explicação no topo do módulo.
        self._lock = threading.RLock()

    # -- conexão --------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Abre uma conexão, confirma ou desfaz, e fecha.

        Uso:
            with db.connect() as conn:
                conn.execute(...)
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- criação ---------------------------------------------------------

    def initialize(self) -> None:
        """Cria as tabelas e os índices. Pode ser chamada em toda subida.

        Todo comando do `schema.sql` usa `IF NOT EXISTS`, então rodar de
        novo em um banco existente não faz nada. É assim que o Monitor
        também funciona: não há "migração" a executar à mão.
        """
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        with self._lock, self.connect() as conn:
            # WAL é propriedade do arquivo e persiste; aplicar aqui basta.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(script)
            conn.execute("PRAGMA optimize")
        logger.info("database ready (%s)", self.path)

    def seed_from_settings(self, settings: Settings) -> None:
        """Cadastra a Base e as coleiras conhecidas a partir do .env.

        Seção 7 da especificação: "A Base de desenvolvimento é cadastrada
        na inicialização a partir do .env, com o token guardado como
        hash" e "Coleiras: cadastradas a partir de COLARES_CONHECIDOS".

        Duas regras diferentes, de propósito:

        - **A Base é atualizada** a cada subida (`ON CONFLICT DO UPDATE`):
          trocar o BASE_TOKEN no .env e reiniciar precisa valer. Mas só
          os campos de configuração — token, latitude e longitude. Os
          campos de estado reportado (`last_heartbeat`, `queue_size`,
          `serial_status`, `fence_version_reported`) são preservados,
          porque pertencem à Base, não ao arquivo de configuração.

        - **As coleiras só são inseridas se não existirem**
          (`INSERT OR IGNORE`): elas carregam estado de verdade (última
          zona, bateria, última posição) que não pode ser apagado a cada
          reinício do servidor.
        """
        token_hash = hash_token(settings.base_token)

        with self._lock, self.connect() as conn:
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
                    INSERT OR IGNORE INTO collars (collar_id, base_id, last_seen)
                    VALUES (?, ?, NULL)
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
    # -- consultas de apoio ---------------------------------------------

    def table_names(self) -> list[str]:
        """Lista as tabelas existentes. Usada pelos testes e no diagnóstico."""
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def get_base(self, base_id: str) -> dict | None:
        """Devolve a Base cadastrada, ou None."""
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bases WHERE base_id = ?", (base_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_collars(self) -> list[dict]:
        """Devolve as coleiras cadastradas, em ordem de identificador."""
        with self._lock, self.connect() as conn:
            rows = conn.execute("SELECT * FROM collars ORDER BY collar_id").fetchall()
        return [dict(row) for row in rows]
