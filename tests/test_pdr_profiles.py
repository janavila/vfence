import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database, MetricSaveResult
from app.pdr_protocol import parse_pdr_summary
from app.serial_reader import GATEWAY_SERIAL_SCHEMA, SerialReader, _SerialWorkQueue
from test_metric_summaries import RecordingHub, hdop_json, metric_rows, radio_json, serial_line


@pytest.fixture(scope="module")
def pdr_frames(tmp_path_factory):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("compilador C++ de host indisponivel")
    root = Path(__file__).resolve().parents[1]
    gateway = root / "gateway/VFenceGateway"
    collar = root / "geofence/VFenceCollarLoRa"
    for name in ("lora_profiles.h", "counted_position.h"):
        assert (gateway / name).read_bytes() == (collar / name).read_bytes()
    binary = tmp_path_factory.mktemp("pdr-cpp") / "test_pdr_profiles"
    subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(gateway),
        str(root / "tests/cpp/test_pdr_profiles.cpp"), str(gateway / "vfence_protocol.cpp"),
        "-o", str(binary),
    ], check=True)
    frames = subprocess.check_output([binary]).splitlines(keepends=True)
    assert len(frames) == 2 and all(frame.startswith(b"[APP] ") and len(frame) < 512 for frame in frames)
    return frames


@pytest.fixture
def pdr_reader(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pdr.db'}")
    database.initialize()
    config = SimpleNamespace(mock_serial=False, serial_port="test", central_sync_enabled=False,
                             max_line_length=512, metrics_gateway_id="GW-HOST")
    return SerialReader(config, database, RecordingHub()), database


@pytest.mark.asyncio
async def test_host_frames_round_trip_and_idempotency(pdr_frames, pdr_reader):
    reader, database = pdr_reader
    first, second = pdr_frames
    assert await reader.process_line(first)
    assert await reader.process_line(first)
    assert await reader.process_line(second.removesuffix(b"\n") + b"\r\n")
    rows = database.list_pdr(254, 2)
    assert len(rows) == 2
    empty, measured = rows
    assert empty["reason"] == "no_samples" and empty["pdr_x100"] is None and not empty["valid"]
    assert measured["gateway_id"] == "GW-HOST" and measured["collar_session_id"] == "000000000000000a"
    assert measured["gateway_session_id"] == "000000000000000b" and measured["estimated_lost_count"] == 1
    assert measured["pdr_x100"] == 6667 and measured["valid"]
    assert not database.list_pdr(1, 10)

    conflict = json.loads(first[6:])
    conflict.update(receivedCount=2, estimatedTxCount=2, estimatedLostCount=0, pdrX100=10000)
    assert database.save_pdr_summary("GW-HOST", parse_pdr_summary(conflict)) is MetricSaveResult.CONFLICT
    assert database.list_pdr(254, 2) == rows


@pytest.mark.asyncio
async def test_pdr_rejects_invalid_wire_and_requires_gateway_identity(pdr_frames, pdr_reader):
    reader, database = pdr_reader
    first = pdr_frames[0]
    assert not await reader.process_line(first.rstrip(b"\n"))
    assert not await reader.process_line(first.replace(b'{"schema"', b'{"collarId":1,"schema"'))
    assert not await reader.process_line(first.replace(b"vfence.gateway.pdr.v1", b"vfence.gateway.serial.v1"))
    assert not await reader.process_line(first[:-2] + b"," + b' "extra": "' + b"a" * 512 + b'"}\n')
    invalid = json.loads(first[6:])
    invalid.update(pdrX100=10000)
    assert not await reader.process_line(serial_line(invalid))
    invalid = json.loads(first[6:])
    invalid.update(estimatedLostCount=True)
    assert not await reader.process_line(serial_line(invalid))
    reader.config.metrics_gateway_id = ""
    assert not await reader.process_line(first)
    assert not database.list_pdr(254, 10)


async def test_short_pdr_does_not_consume_next_radio_window(pdr_frames, pdr_reader):
    reader, database = pdr_reader
    prefix = pdr_frames[0].removesuffix(b"\n")
    reader.config.metrics_installation_id = "INSTALL-HOST"
    await reader._process_serial_chunk(prefix)
    await reader._process_serial_chunk(b"")
    await reader._process_serial_chunk(b"\0")
    await reader._process_serial_chunk(b"")
    await reader._process_serial_chunk(b"\n" + serial_line(radio_json(collarId=7), b"\r\n"))
    assert database.list_pdr(254, 10) == []
    radio, = metric_rows(database)
    assert radio["kind"] == "RADIO" and radio["collar_id"] == 7
    assert radio["session_id"] == "8000000000000000"
    assert radio["window_id"] == "fffffffffffffffe"


def position_line(sequence):
    collar = sequence % 3 + 1
    return f"V1|COL{collar:02d}|POS|-31|-54|SEGURO|9|0.90|{sequence}\n"


@pytest.mark.parametrize(("occupancy", "pdr_admitted", "reason"), [
    (0, True, None), (40, True, None), (119, True, None),
    (120, False, "operational reserve"), (127, False, "operational reserve"),
    (128, False, "global capacity"),
])
async def test_pdr_admission_reserves_operational_capacity(
    occupancy, pdr_admitted, reason, pdr_frames, pdr_reader, caplog,
):
    reader, database = pdr_reader
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128)
    for sequence in range(1, occupancy + 1):
        await reader.process_line(position_line(sequence))
    assert await reader.process_line(pdr_frames[0]) == pdr_admitted
    assert queue.qsize() == occupancy + pdr_admitted
    assert reader._refused_pdr == (not pdr_admitted)
    if reason:
        assert reason in caplog.text
    assert await reader.process_line(position_line(occupancy + 1)) == (occupancy < 128)
    assert reader._refused_legacy == (occupancy == 128)
    assert queue.qsize() == min(128, occupancy + pdr_admitted + 1)
    assert database.list_pdr(254, 10) == []
    if occupancy == 128:
        assert await reader.process_line(serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "event",
                                                     "subtype": "OUTSIDE", "collarId": 3}))
        assert queue.qsize() == 129 and reader._queued_critical == 1
        assert reader._refused_legacy == 1 and reader._evicted_pdr == 0
        assert reader.hub.messages == []


async def test_queued_pdr_yields_to_operational_and_metric_work(pdr_frames, pdr_reader, caplog):
    reader, database = pdr_reader
    reader.config.metrics_installation_id = "INSTALL-HOST"
    queue = reader._work_queue = _SerialWorkQueue(maxsize=128)
    pdr = json.loads(pdr_frames[0][6:])
    for window in range(1, 10):
        await reader.process_line(serial_line({
            **pdr, "collarId": 7 if window % 2 else 11, "windowId": f"{window:016x}",
        }))
    assert reader._queued_pdr == 8 and reader._refused_pdr == 1
    assert "PDR quota" in caplog.text
    await reader.process_line(serial_line(hdop_json(collarId=7)))
    await reader.process_line(serial_line(radio_json(collarId=11)))
    for sequence in range(1, 119):
        await reader.process_line(position_line(sequence))
    assert queue.qsize() == queue.regular_capacity

    await reader.process_line(serial_line(hdop_json(collarId=11)))
    await reader.process_line(serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "event",
                                           "subtype": "OUTSIDE", "collarId": 3}))
    await reader.process_line(position_line(119))
    assert queue.qsize() == 129 and reader._queued_metrics == 3 and reader._queued_pdr == 6
    assert reader._evicted_pdr == 2 and reader._refused_legacy == reader._refused_metrics == 0

    loop = asyncio.get_running_loop()
    command = reader._pending_commands[91] = loop.create_future()
    fence = reader._fence_result = loop.create_future()
    reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 92, 3, 7
    await reader.process_line(serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "command_response",
                                           "requestId": 91, "command": "GET_PROFILE", "ok": True}))
    await reader.process_line(serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "fence_transfer",
                                           "requestId": 92, "collarId": 3, "fenceVersion": 7, "state": "confirmed"}))
    assert command.result()["ok"] is True and fence.result() == (True, "collar_confirmed")

    worker = asyncio.create_task(reader._work_loop(queue))
    await reader._finish_work(queue, worker)
    assert reader._queued_pdr == 0 and reader._processed_work == 129
    assert reader._processed_work + reader._evicted_pdr == reader._admitted_work
    retained = database.list_pdr(7, 10) + database.list_pdr(11, 10)
    assert sorted(int(item["window_id"], 16) for item in retained) == [1, 2, 3, 4, 5, 6]
    assert len(metric_rows(database)) == 3
    assert sum(len(database.telemetry(f"COL{i:02}")) for i in (1, 2, 3)) == 119
    assert any(event.get("data", {}).get("subtype") == "OUTSIDE" for event in reader.hub.messages)
    assert sum(row["event_type"] == "outside" for row in database.events()) == 1


async def test_pdr_commit_v2_requires_a_commit_and_preserves_legacy_wire(pdr_frames, pdr_reader):
    from test_metric_serial_export import MetricConnection

    reader, database = pdr_reader
    reader.config.pdr_serial_commit_enabled = True
    reader._connection = port = MetricConnection()
    legacy = pdr_frames[0]
    assert await reader.process_line(legacy)
    assert port.writes == []
    payload = {**json.loads(legacy[6:]), "schema": "vfence.gateway.pdr.v2"}
    assert await reader.process_line(serial_line(payload))
    assert await reader.process_line(serial_line(payload))
    assert len(port.writes) == 2 and len(database.list_pdr(payload["collarId"], 10)) == 1
    first = json.loads(port.writes[0][6:])
    assert first["command"] == "PDR_COMMITTED"
    assert first["windowId"] == payload["windowId"] and first["gatewaySessionId"] == payload["gatewaySessionId"]
    assert not await reader.process_line(serial_line({**payload, "startedAtMs": payload["startedAtMs"] ^ 1}))
    assert len(port.writes) == 2


async def test_link_metrics_keeps_independent_windows_and_unknown_profile(pdr_frames, pdr_reader, monkeypatch):
    from app import main

    reader, database = pdr_reader
    reader.config.metrics_installation_id = "INSTALL-HOST"
    monkeypatch.setattr(main, "reader", reader)
    monkeypatch.setattr(main, "database", database)
    assert await reader.process_line(serial_line(hdop_json(collarId=254)))
    assert await reader.process_line(serial_line(radio_json(collarId=254)))
    assert await reader.process_line(pdr_frames[0])
    snapshot = await main.collar_link_metrics(254, 10)
    assert snapshot["schema"] == "vfence.base.link_metrics.v1"
    assert snapshot["gateway"]["effective_profile"] is None and snapshot["instant"] is None
    assert snapshot["pdr_basis"] == "observed_ordinal_interval"
    assert set(snapshot["windows"]) == {"hdop", "radio", "pdr"}
    assert all(len(rows) == 1 and rows[0]["age_ms"] >= 0 for rows in snapshot["windows"].values())


def test_read_only_pdr_and_profile_api(monkeypatch, pdr_frames, pdr_reader):
    from fastapi.testclient import TestClient
    from app import main

    _, database = pdr_reader
    assert database.save_pdr_summary("GW-HOST", parse_pdr_summary(json.loads(pdr_frames[0][6:]))) is MetricSaveResult.INSERTED
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "settings", SimpleNamespace(mock_serial=False))
    send = AsyncMock(return_value={"ok": True, "command": "GET_PROFILE", "profileControlVersion": 1,
                                   "activeProfileId": 0, "state": "IDLE", "switchEnabled": False})
    reader = SimpleNamespace(state=SimpleNamespace(status="online"), send_gateway_command=send)
    monkeypatch.setattr(main, "reader", reader)
    client = TestClient(main.app)
    assert client.get("/api/collars/254/pdr").json()[0]["pdr_x100"] == 6667
    assert client.get("/api/collars/1/pdr").json() == []
    assert client.get("/api/collars/255/pdr").status_code == 422
    assert client.get("/api/gateway/profile").json()["switchEnabled"] is False
    send.assert_awaited_once_with("GET_PROFILE")
    send.return_value = {"ok": False, "detail": "command_unknown"}
    assert client.get("/api/gateway/profile").status_code == 501
    reader.state.status = "offline"
    assert client.get("/api/gateway/profile").status_code == 503
