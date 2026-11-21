import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database, MetricSaveResult
from app.pdr_protocol import parse_pdr_summary
from app.serial_reader import SerialReader
from test_metric_summaries import RecordingHub, metric_rows, radio_json, serial_line


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
    assert await reader.process_line(first)
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
