import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.central_client import CentralClient, CentralClientError
from app.database import Database
from app.serial_reader import GATEWAY_SERIAL_SCHEMA, SerialReader, _SerialWorkQueue
from app.sync_service import CentralSyncService
from test_central_integration import base_config
from test_metric_serial_export import until
from test_metric_summaries import RecordingHub, serial_line
from test_pdr_profiles import position_line


def gateway_event(name="OUTSIDE", *, sequence=44, collar=3):
    return {
        "schema": GATEWAY_SERIAL_SCHEMA, "kind": "event", "subtype": name,
        "collarId": collar, "sequence": sequence, "fenceVersion": 12,
    }


def outside_rows(db):
    return [row for row in db.events() if row["event_type"] == "outside"]


@pytest.fixture
def local_base(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'base.db'}")
    db.initialize()
    return SerialReader(base_config(), db, RecordingHub()), db


async def test_outside_uses_reserved_capacity_and_commits_ahead_of_128_positions(local_base):
    reader, db = local_base
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128)
    for sequence in range(128):
        assert await reader.process_line(position_line(sequence))
    assert not await reader.process_line(position_line(129))
    event = gateway_event()
    assert await reader.process_line(serial_line(event))
    assert queue.qsize() == 129 and outside_rows(db) == [] and not reader.hub.messages
    await reader._finish_work(queue, asyncio.create_task(reader._work_loop(queue)))
    assert reader.hub.messages[0] == {"type": "gateway_event", "data": event}
    assert len(outside_rows(db)) == 1 and json.loads(outside_rows(db)[0]["new_value"]) == event
    assert db.outbox_batch()[0]["data"]["kind"] == "outside"
    assert sum(len(db.telemetry(f"COL{i:02}")) for i in (1, 2, 3)) == 128


async def test_critical_burst_is_fifo_and_refusal_is_visible_and_true_to_admission(local_base, caplog):
    reader, db = local_base
    reader.terminal_detail = True
    reader.config.central_sync_enabled = False
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128)
    names = ("OUTSIDE", "GPS_LOST", "GPS_RECOVERED", "BACK_INSIDE", "LOW_BATTERY", "CRITICAL_BATTERY")
    frames = [gateway_event(names[i % 6], sequence=i, collar=i % 4 + 1) for i in range(16)]
    with caplog.at_level(logging.INFO):
        for frame in frames:
            assert await reader.process_line(serial_line(frame))
        assert not await reader.process_line(serial_line(gateway_event(sequence=44)))
        assert queue.qsize() == 16
        await reader._finish_work(queue, asyncio.create_task(reader._work_loop(queue)))
    assert [message["data"] for message in reader.hub.messages] == frames
    assert len(db.events()) == 16 and db.outbox_size() == 0
    assert "ADMITIDO" in caplog.text and "RECUSADO POR SATURACAO" in caplog.text
    assert "Criticos recusados: 1" in caplog.text and "refused=1" in caplog.text
    assert "COMMIT CONCLUIDO | Central: SINCRONIZACAO DESATIVADA" in caplog.text


async def test_refused_legacy_frame_at_idle_is_consumed_without_resubmission(local_base):
    reader, db = local_base
    queue = reader._work_queue = _SerialWorkQueue(maxsize=1)
    assert await reader.process_line(position_line(1))
    await reader._process_serial_chunk(position_line(2).strip().encode())
    await reader._process_serial_chunk(b"")
    await reader._process_serial_chunk(b"")
    assert reader._refused_legacy == 1 and reader._discard_until_lf
    worker = asyncio.create_task(reader._work_loop(queue))
    await queue.join()
    await reader._process_serial_chunk(b"invalid physical suffix\n" + position_line(3).encode())
    await reader._finish_work(queue, worker)
    assert db.telemetry("COL03") == []
    assert db.telemetry("COL02")[0]["sequence"] == 1
    assert db.telemetry("COL01")[0]["sequence"] == 3


async def test_graceful_shutdown_drains_events_after_regular_timeout(local_base, monkeypatch, caplog):
    reader, db = local_base
    monkeypatch.setattr("app.serial_reader._WORK_DRAIN_SECONDS", 0.02)
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128)
    started, release = asyncio.Event(), asyncio.Event()

    async def held_position():
        started.set()
        await release.wait()

    await reader._submit_work(held_position)
    worker = asyncio.create_task(reader._work_loop(queue))
    await asyncio.wait_for(started.wait(), 1)
    assert await reader.process_line(position_line(1))
    assert await reader.process_line(serial_line(gateway_event()))
    finish = asyncio.create_task(reader._finish_work(queue, worker))
    try:
        await until(lambda: "discarded on shutdown (1 items, 0 metrics)" in caplog.text)
        assert outside_rows(db) == []
    finally:
        release.set()
        await asyncio.wait_for(finish, 2)
    assert len(outside_rows(db)) == 1
    assert "discarded on shutdown (1 items, 0 metrics)" in caplog.text
    assert db.telemetry("COL02") == []


async def test_locked_sqlite_retries_before_broadcast(local_base, monkeypatch):
    reader, db = local_base
    busy = threading.Event()
    save = db.save_gateway_event

    def observe_lock(*args, **kwargs):
        try:
            return save(*args, **kwargs)
        except sqlite3.OperationalError:
            busy.set()
            raise

    monkeypatch.setattr(db, "save_gateway_event", observe_lock)
    with closing(sqlite3.connect(db.path)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(reader.process_line(serial_line(gateway_event())))
        try:
            assert await asyncio.to_thread(busy.wait, 2)
            assert reader.hub.messages == []
        finally:
            lock.rollback()
            result = await asyncio.wait_for(task, 2)
    assert result and len(outside_rows(db)) == len(reader.hub.messages) == db.outbox_size() == 1


@pytest.mark.parametrize("queued", [False, True])
async def test_failed_outbox_write_rolls_back_event_and_logs_failure(local_base, caplog, queued):
    reader, db = local_base
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_outbox BEFORE INSERT ON central_outbox
                        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128) if queued else None
    assert await reader.process_line(serial_line(gateway_event())) == queued
    if queue is not None:
        await reader._finish_work(queue, asyncio.create_task(reader._work_loop(queue)))
        assert reader._failed_work == 1
    assert outside_rows(db) == [] and db.outbox_size() == 0 and reader.hub.messages == []
    assert "FALHA DE PERSISTENCIA" in caplog.text and "COMMIT CONCLUIDO" not in caplog.text


async def test_serial_replay_has_no_stable_source_identity_after_base_restart(local_base):
    reader, db = local_base
    reader.config.central_sync_enabled = False
    frame = serial_line(gateway_event())
    assert await reader.process_line(frame)
    reopened = Database(f"sqlite:///{db.path}")
    restarted = SerialReader(reader.config, reopened, RecordingHub())
    reopened.initialize()
    rows = outside_rows(reopened)
    assert len(rows) == 1 and restarted.hub.messages == []
    assert await restarted.process_line(frame)
    # The Gateway does not supply a boot session for OUTSIDE. Equal frames can be distinct incidents.
    rows = outside_rows(reopened)
    assert len(rows) == 2 and len({row["id"] for row in rows}) == 2 and reopened.outbox_size() == 0


async def test_reliable_gateway_event_reacked_after_restart_and_conflict_is_refused(local_base):
    from test_metric_serial_export import MetricConnection

    reader, db = local_base
    reader.config.event_serial_commit_enabled = True
    payload = {**gateway_event(), "deliveryVersion": 1, "deliveryId": "1234567890abcdef00000001"}
    port = MetricConnection()
    reader._connection = port
    assert await reader.process_line(serial_line(payload))
    assert len(outside_rows(db)) == db.outbox_size() == len(port.writes) == 1
    outgoing = json.loads(port.writes[0][6:])
    assert outgoing["command"] == "EVENT_COMMITTED" and outgoing["deliveryId"] == payload["deliveryId"]

    reopened = Database(f"sqlite:///{db.path}")
    restarted = SerialReader(reader.config, reopened, RecordingHub())
    restarted._connection = port
    assert await restarted.process_line(serial_line(payload))
    assert not await restarted.process_line(serial_line({**payload, "sequence": 99}))
    assert len(port.writes) == 2 and len(outside_rows(reopened)) == reopened.outbox_size() == 1
    assert restarted.hub.messages == []


async def test_central_retry_and_lost_ack_deduplicate_the_same_outbox_identity(local_base, tmp_path, monkeypatch):
    reader, db = local_base
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "web"))
    from backend.db import Database as CentralDatabase
    from backend.services.edge_service import process_batch

    central = CentralDatabase(tmp_path / "central.db")
    central.initialize()
    settings = SimpleNamespace(base_id=reader.config.base_id, base_token="test-base-token",
                               base_lat=0.0, base_lon=0.0, known_collars=())
    central.seed_from_settings(settings)
    attempts, published = [], []

    def respond(request):
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"desired_fence_version": None, "server_time": "test"})
        batch = json.loads(request.content)
        attempts.append(batch)
        if len(attempts) == 1:
            raise httpx.ConnectError("Central offline", request=request)
        ack, messages = process_batch(central, settings, batch["base_id"], batch["items"],
                                      generation=batch["generation"])
        published.extend(messages)
        if len(attempts) == 2:
            raise httpx.ReadTimeout("ack lost after commit", request=request)
        return httpx.Response(200, json={"generation": batch["generation"], "acked_sequences": ack})

    client = CentralClient(reader.config.central_base_url, settings.base_token, 1, httpx.MockTransport(respond))
    assert await reader.process_line(serial_line(gateway_event()))
    assert len(outside_rows(db)) == 1 and db.outbox_size() == 1
    service = CentralSyncService(reader.config, db, reader, reader.hub, client)
    with pytest.raises(CentralClientError):
        await service.sync_once()
    assert len(outside_rows(db)) == 1 and db.outbox_size() == 1 and not published

    reopened = Database(f"sqlite:///{db.path}")
    restarted = CentralSyncService(reader.config, reopened, reader, reader.hub, client)
    with pytest.raises(CentralClientError):
        await restarted.sync_once()
    assert len(published) == 1 and reopened.outbox_size() == 1
    await restarted.sync_once()
    assert attempts[0] == attempts[1] == attempts[2]
    assert len(published) == 1 and reopened.outbox_size() == 0 and len(outside_rows(reopened)) == 1
    with central.connect() as conn:
        assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "outside"
        assert conn.execute("SELECT COUNT(*) AS total FROM events").fetchone()["total"] == 1
