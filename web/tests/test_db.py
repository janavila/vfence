"""Testes do banco (item CT-02 do backlog: "Teste cria e lê uma cerca").

Estes testes conferem o que a fase F1 promete do lado dos dados: as nove
tabelas da seção 7 existem, a Base e as coleiras do `.env` são
cadastradas na subida, uma cerca com seus pontos pode ser gravada e lida
de volta, e a premissa P3 ("uma cerca ativa por vez") é garantida pelo
próprio banco.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.clock import utc_now_iso
from backend.db import Database
from backend.security import hash_token

# As nove tabelas da seção 7 da especificação.
TABELAS_ESPERADAS = {
    "fences",
    "fence_points",
    "bases",
    "collars",
    "deliveries",
    "edge_items",
    "telemetry",
    "events",
    "fence_log_lines",
}

# Cerca de exemplo da seção 5.5, em microgradus e no sentido horário.
# É a mesma cerca cujo CRC (DA29321E) foi conferido na fase F0 e que vai
# virar vetor de teste da geometria na fase F2.
PONTOS_EXEMPLO_E6 = [
    (-31306000, -54064200),
    (-31306000, -54063700),
    (-31306400, -54063700),
    (-31306400, -54064200),
]


# ---------------------------------------------------------------------------
# Criação do banco
# ---------------------------------------------------------------------------


def test_todas_as_tabelas_da_especificacao_sao_criadas(database: Database):
    existentes = set(database.table_names())

    assert TABELAS_ESPERADAS <= existentes, (
        f"faltando: {sorted(TABELAS_ESPERADAS - existentes)}"
    )


def test_initialize_pode_rodar_de_novo_sem_estragar_nada(database: Database):
    """O schema é aplicado em toda subida do servidor, então tem de ser
    inofensivo quando o banco já existe."""
    database.initialize()
    database.initialize()

    assert TABELAS_ESPERADAS <= set(database.table_names())


def test_chaves_estrangeiras_estao_ligadas(database: Database):
    """Sem o PRAGMA, o SQLite aceitaria um ponto apontando para cerca
    inexistente — e a cerca ficaria órfã sem ninguém perceber."""
    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO fence_points (fence_id, seq, lat_e6, lon_e6) VALUES (?,?,?,?)",
                (9999, 1, -31306000, -54064200),
            )


# ---------------------------------------------------------------------------
# Cadastro inicial a partir do .env
# ---------------------------------------------------------------------------


def test_base_do_env_e_cadastrada_na_subida(database: Database, settings):
    base = database.get_base(settings.base_id)

    assert base is not None
    assert base["base_id"] == settings.base_id
    assert base["lat"] == pytest.approx(-31.306200)
    assert base["lon"] == pytest.approx(-54.063950)
    # Ainda não houve heartbeat: o estado reportado começa vazio.
    assert base["last_heartbeat"] is None
    assert base["fence_version_reported"] is None


def test_o_token_da_base_nunca_e_guardado_em_texto_puro(database: Database, settings):
    """O banco guarda o hash, não o segredo (seção 10 do planejamento)."""
    base = database.get_base(settings.base_id)

    assert base["token_hash"] == hash_token(settings.base_token)
    assert settings.base_token not in base["token_hash"]
    assert len(base["token_hash"]) == 64  # SHA-256 em hexadecimal


def test_coleiras_conhecidas_do_env_sao_cadastradas(database: Database, settings):
    coleiras = database.list_collars()

    assert [c["collar_id"] for c in coleiras] == list(settings.known_collars)
    assert all(c["base_id"] == settings.base_id for c in coleiras)
    # Nenhuma zona inventada: o Central não decide zona.
    assert all(c["last_zone"] is None for c in coleiras)


def test_novo_cadastro_preserva_o_estado_das_coleiras(database: Database, settings):
    """Reiniciar o servidor não pode apagar a última posição conhecida."""
    with database.connect() as conn:
        conn.execute(
            "UPDATE collars SET last_zone = 'SEGURO', battery_pct = 87 WHERE collar_id = ?",
            ("COL01",),
        )

    database.seed_from_settings(settings)  # simula uma nova subida

    coleira = next(c for c in database.list_collars() if c["collar_id"] == "COL01")
    assert coleira["last_zone"] == "SEGURO"
    assert coleira["battery_pct"] == 87


def test_novo_cadastro_atualiza_o_token_da_base(database: Database, settings, monkeypatch):
    """Trocar BASE_TOKEN no .env e reiniciar precisa valer."""
    from backend.config import load_settings

    monkeypatch.setenv("BASE_TOKEN", "token-novo")
    database.seed_from_settings(load_settings(env_file=None))

    assert database.get_base(settings.base_id)["token_hash"] == hash_token("token-novo")


# ---------------------------------------------------------------------------
# CT-02: criar e ler uma cerca
# ---------------------------------------------------------------------------


def test_cria_e_le_uma_cerca_com_seus_pontos(database: Database):
    """O caminho completo de ida e volta de uma cerca no banco.

    Ainda sem API e sem validação — isso é a fase F3. Aqui provamos só
    que o modelo de dados da seção 7 comporta uma cerca de verdade.
    """
    agora = utc_now_iso()

    with database.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO fences (version, name, status, margin_attention_cm,
                                margin_critical_cm, area_m2, perimeter_m,
                                crc32, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (1, "Piquete A", "active", 500, 200, 2112.83, 183.96, "DA29321E", agora),
        )
        fence_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO fence_points (fence_id, seq, lat_e6, lon_e6) VALUES (?,?,?,?)",
            [(fence_id, seq, lat, lon) for seq, (lat, lon) in enumerate(PONTOS_EXEMPLO_E6, start=1)],
        )

    with database.connect() as conn:
        cerca = dict(conn.execute("SELECT * FROM fences WHERE version = 1").fetchone())
        pontos = [
            (row["lat_e6"], row["lon_e6"])
            for row in conn.execute(
                "SELECT lat_e6, lon_e6 FROM fence_points WHERE fence_id = ? ORDER BY seq",
                (fence_id,),
            )
        ]

    assert cerca["name"] == "Piquete A"
    assert cerca["status"] == "active"
    assert cerca["crc32"] == "DA29321E"
    assert cerca["created_at"].endswith("Z")
    # Margens em centímetros inteiros, nunca float (seção 5.5).
    assert (cerca["margin_attention_cm"], cerca["margin_critical_cm"]) == (500, 200)
    assert cerca["warnings_json"] == "[]"  # valor padrão da tabela
    # Pontos voltam em microgradus inteiros, na ordem de desenho.
    assert pontos == PONTOS_EXEMPLO_E6
    assert all(isinstance(valor, int) for ponto in pontos for valor in ponto)


def test_a_versao_da_cerca_nao_pode_repetir(database: Database):
    """"Versões só crescem" (seção 3.3 do planejamento): a unicidade é
    garantida pelo banco, não só pelo código."""
    agora = utc_now_iso()
    linha = (1, "Piquete A", "inactive", 500, 200, 10.0, 20.0, "DA29321E", agora)
    comando = """
        INSERT INTO fences (version, name, status, margin_attention_cm,
                            margin_critical_cm, area_m2, perimeter_m, crc32, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """

    with database.connect() as conn:
        conn.execute(comando, linha)

    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute(comando, linha)


def test_so_pode_existir_uma_cerca_ativa(database: Database):
    """Premissa P3 do planejamento, garantida pelo índice único parcial.

    Se um erro de programação tentar ativar duas cercas, o banco recusa
    na hora — em vez de o produtor descobrir depois que metade do
    rebanho está com a cerca errada.
    """
    agora = utc_now_iso()
    comando = """
        INSERT INTO fences (version, name, status, margin_attention_cm,
                            margin_critical_cm, area_m2, perimeter_m, crc32, created_at)
        VALUES (?,?,'active',500,200,10.0,20.0,'DA29321E',?)
    """

    with database.connect() as conn:
        conn.execute(comando, (1, "Piquete A", agora))

    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute(comando, (2, "Piquete B", agora))


def test_status_de_cerca_so_aceita_active_ou_inactive(database: Database):
    """O CHECK da seção 7 impede um status inventado."""
    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO fences (version, name, status, margin_attention_cm,
                                    margin_critical_cm, area_m2, perimeter_m, crc32, created_at)
                VALUES (1,'Piquete A','arquivada',500,200,10.0,20.0,'DA29321E',?)
                """,
                (utc_now_iso(),),
            )


def test_status_de_entrega_so_aceita_os_cinco_estados(database: Database):
    """Os estados da seção 8: pending, at_base, transmitting, confirmed, failed."""
    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO deliveries (fence_version, collar_id, status, updated_at) VALUES (1,'COL01','enviada',?)",
                (utc_now_iso(),),
            )


# ---------------------------------------------------------------------------
# Isolamento dos testes
# ---------------------------------------------------------------------------


def test_o_banco_de_teste_fica_na_pasta_temporaria(database: Database, data_dir):
    """Garante que nenhum teste escreve no banco de verdade."""
    assert database.path == data_dir / "vfence.db"
    assert database.path.exists()
