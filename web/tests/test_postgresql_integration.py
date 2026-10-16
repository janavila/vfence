"""Integração opcional do VFence Central com PostgreSQL real.

Para executar, aponte ``TEST_DATABASE_URL`` para um banco EXCLUSIVO de teste.
As tabelas do schema VFence são esvaziadas antes e depois de cada teste:

    $env:TEST_DATABASE_URL="postgresql://.../vfence_test?sslmode=require"
    python -m pytest tests/test_postgresql_integration.py -q

Sem a variável, este módulo é pulado sem acessar rede nem inventar credenciais.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.clock import utc_now_iso
from backend.db import Database
from backend.main import create_app
from backend.security import SESSION_COOKIE, create_session_token


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL não configurada; integração PostgreSQL não executada",
)

TABLES = (
    "fence_points",
    "deliveries",
    "edge_items",
    "telemetry",
    "events",
    "fence_log_lines",
    "collars",
    "bases",
    "fences",
)


def _truncate(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE")


@pytest.fixture
def pg_settings(settings):
    if not TEST_DATABASE_URL.startswith(("postgresql://", "postgres://")):
        pytest.fail("TEST_DATABASE_URL deve usar postgresql:// ou postgres://")
    return replace(settings, database_url=TEST_DATABASE_URL)


@pytest.fixture
def pg_db(pg_settings):
    db = Database(pg_settings.database_target)
    db.initialize()
    _truncate(db)
    db.seed_from_settings(pg_settings)
    yield db
    _truncate(db)


@pytest.fixture
def pg_client(pg_settings, pg_db):
    app = create_app(settings=pg_settings, log_to_file=False)
    token = create_session_token(
        pg_settings.admin_user, pg_settings.session_secret, 3600
    )
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        yield client


@pytest.fixture
def pg_headers(pg_settings):
    return {"Authorization": f"Bearer {pg_settings.base_token}"}


def test_postgresql_inicializa_schema_idempotente(pg_db: Database):
    pg_db.initialize()
    pg_db.initialize()

    assert set(TABLES) <= set(pg_db.table_names())


def test_postgresql_fluxo_cerca_heartbeat_download_e_batch(
    pg_client, pg_headers, cerca_limpa
):
    first = pg_client.post("/api/fences", json=cerca_limpa)
    assert first.status_code == 201
    fence = first.json()
    assert fence["version"] == 1
    assert len(fence["points"]) == len(cerca_limpa["points"])

    persisted = pg_client.get("/api/fences/1")
    assert persisted.status_code == 200
    assert persisted.json()["crc32"] == fence["crc32"]
    assert persisted.json()["points"] == fence["points"]

    second = pg_client.post(
        "/api/fences", json={**cerca_limpa, "name": "Piquete B"}
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2

    heartbeat = pg_client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01", "fence_version": 1},
        headers=pg_headers,
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["desired_fence_version"] == 2

    download = pg_client.get("/api/edge/fences/2", headers=pg_headers)
    assert download.status_code == 200
    wire_fence = download.json()
    assert wire_fence["crc32"] == second.json()["crc32"]
    assert wire_fence["points_e6"][0] == [-31305800, -54064600]

    batch = pg_client.post(
        "/api/edge/batch",
        headers=pg_headers,
        json={
            "base_id": "BASE01",
            "items": [
                {
                    "edge_seq": 1,
                    "type": "telemetry",
                    "data": {
                        "collar_id": "COL01",
                        "ts": "2026-10-06T13:01:58Z",
                        "lat": -31.306119,
                        "lon": -54.063935,
                        "zone": "SEGURO",
                        "fence_version": 2,
                    },
                },
                {
                    "edge_seq": 2,
                    "type": "event",
                    "data": {
                        "collar_id": "COL01",
                        "kind": "zone_change",
                        "zone": "ATENCAO",
                        "ts": "2026-10-06T13:02:00Z",
                    },
                },
                {
                    "edge_seq": 3,
                    "type": "delivery",
                    "data": {
                        "collar_id": "COL01",
                        "fence_version": 2,
                        "status": "confirmed",
                        "crc32": wire_fence["crc32"],
                    },
                },
            ],
        },
    )
    assert batch.status_code == 200
    assert batch.json() == {"acked_up_to": 3}

    invalid = pg_client.post(
        "/api/edge/heartbeat",
        json={"base_id": "BASE01"},
        headers={"Authorization": "Bearer token-invalido"},
    )
    assert invalid.status_code == 401


def test_postgresql_rollback_nao_deixa_cerca_sem_pontos(pg_db: Database):
    with pytest.raises(RuntimeError):
        with pg_db.connect() as conn:
            conn.execute(
                """
                INSERT INTO fences
                    (version, name, status, margin_attention_cm, margin_critical_cm,
                     area_m2, perimeter_m, crc32, created_at)
                VALUES (1, 'Rollback', 'active', 800, 500, 1, 1, '00000000', ?)
                """,
                (utc_now_iso(),),
            )
            raise RuntimeError("falha simulada antes dos pontos")

    with pg_db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS total FROM fences WHERE version = 1"
        ).fetchone()["total"]
    assert count == 0


def test_postgresql_reinicio_da_aplicacao_preserva_cerca(
    pg_settings, pg_db, cerca_limpa
):
    token = create_session_token(
        pg_settings.admin_user, pg_settings.session_secret, 3600
    )
    first_app = create_app(settings=pg_settings, log_to_file=False)
    with TestClient(first_app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        created = client.post("/api/fences", json=cerca_limpa).json()

    second_app = create_app(settings=pg_settings, log_to_file=False)
    with TestClient(second_app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        loaded = client.get(f"/api/fences/{created['version']}")

    assert loaded.status_code == 200
    assert loaded.json()["crc32"] == created["crc32"]


def test_postgresql_versao_e_segura_entre_duas_conexoes(pg_db: Database):
    versions: list[int] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(2)

    def reserve(name: str) -> None:
        try:
            barrier.wait(timeout=5)
            with pg_db.connect() as conn:
                version = pg_db.next_fence_version(conn)
                conn.execute("UPDATE fences SET status = 'inactive' WHERE status = 'active'")
                conn.execute(
                    """
                    INSERT INTO fences
                        (version, name, status, margin_attention_cm, margin_critical_cm,
                         area_m2, perimeter_m, crc32, created_at)
                    VALUES (?,?,'active',800,500,1,1,'00000000',?)
                    """,
                    (version, name, utc_now_iso()),
                )
                time.sleep(0.1)
                versions.append(version)
        except Exception as exc:  # pragma: no cover - só aparece como detalhe útil
            failures.append(exc)

    threads = [
        threading.Thread(target=reserve, args=("A",)),
        threading.Thread(target=reserve, args=("B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(versions) == [1, 2]
