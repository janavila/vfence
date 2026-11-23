import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.database import Database
from app.metric_protocol import MetricSummary
from app.pdr_protocol import PdrSummary
from app.serial_reader import SerialReader


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'monitor.sqlite'}")
    db.initialize()
    hub = AsyncMock()
    reader = SerialReader(Settings(mock_serial=False, central_sync_enabled=False), db, hub)
    monkeypatch.setattr(main, "reader", reader)
    monkeypatch.setattr(main, "database", db)
    return db, reader, hub, TestClient(main.app)


def frame(kind, **fields):
    return "[APP] " + json.dumps({"schema": "vfence.gateway.serial.v1", "kind": kind, **fields}) + "\n"


@pytest.mark.parametrize("fields", [
    {"gpsValid": "false"}, {"latitude": 92}, {"rssi": float("nan")}, {"sequence": -1},
])
async def test_invalid_position_does_not_enter_storage(fields, monitor):
    db, reader, _, _ = monitor
    payload = {"collarId": 1, "sequence": 1, "gpsValid": True, "latitude": 0, "longitude": 0}
    assert not await reader.process_line(frame("telemetry", **(payload | fields)))
    assert db.list_collars() == []
    assert reader.last_response_at is None


async def test_monitor_separates_port_response_current_position_and_last_fix(monitor):
    db, reader, hub, client = monitor
    assert client.get("/api/gateway").json()["serial_open"] is False
    reader._connection = object()
    reader.state.status = "online"
    assert client.get("/api/gateway").json()["last_response_at"] is None
    now = datetime.now(timezone.utc)
    position = {"collarId": 1, "sequence": 9, "latitude": -31, "longitude": -54,
                "gpsValid": True, "inside": True, "hdop": 1.4, "rssi": -95, "fenceVersion": 4}
    await reader._process_gateway_app({"kind": "telemetry", **position}, now - timedelta(seconds=2))
    assert client.get("/api/gateway").json()["last_response_at"] is not None
    assert client.get("/api/collars").json()[0]["contact"]["state"] == "online"
    assert client.get("/api/collars/COL01").json()["position"]["fence_version"] == 4

    await reader.process_line(frame("event", collarId=1, subtype="GPS_LOST"))
    assert client.get("/api/collars/COL01").json()["gnss_event"]["type"] == "gps_lost"
    await reader.process_line(frame("telemetry", collarId=1, sequence=10, gpsValid=False, snr=0))
    row = client.get("/api/collars/COL01").json()
    assert row["position"]["zone"] == "GNSS_INVALIDO"
    assert row["position"]["lat"] is row["position"]["hdop"] is row["position"]["rssi"] is None
    assert row["position"]["snr"] == 0
    assert row["last_fix"]["lat"] == -31
    telemetry = hub.broadcast.await_args.args[0]
    assert telemetry["collar"] == row
    assert telemetry["gateway"]["last_seen"] == client.get("/api/gateway").json()["last_seen"]
    assert len(db.telemetry("COL01")) == 2

    await reader.process_line(frame("collar_state", collarId=1, state="OFFLINE"))
    assert client.get("/api/collars").json()[0]["contact"]["state"] == "offline"
    await reader._process_gateway_app({"kind": "telemetry", **position}, now - timedelta(seconds=1))
    row = client.get("/api/collars").json()[0]
    assert row["contact"]["state"] == "offline"
    assert row["position"]["sequence"] == 10


async def test_monitor_windows_use_persisted_scales(monitor):
    _, reader, hub, client = monitor
    assert client.get("/api/collars/1/link-metrics").json()["windows"] == {"hdop": [], "radio": [], "pdr": []}
    session, window = "a" * 16, "0000000000000001"
    await reader._persist_metric_summary(MetricSummary(
        "HDOP", "COLLAR", 1, session, window, 1000, 60000, 54,
        hdop_mean_x100=142, hdop_mean_valid=True,
    ))
    await reader._persist_metric_summary(MetricSummary(
        "RADIO", "GATEWAY", 1, session, window, 9000, 60000, 12,
        rssi_mean_x100=-9542, snr_mean_x100=0, rssi_mean_valid=True, snr_mean_valid=True,
    ))
    await reader._persist_pdr_summary("GATEWAY", PdrSummary(
        1, session, window, session, 9000, 60000, 2, 6, 8, 2, 7500, True, "ok",
    ))
    windows = client.get("/api/collars/1/link-metrics?limit=1").json()["windows"]
    assert windows["hdop"][0]["hdop_mean_x100"] == 142
    assert windows["radio"][0]["rssi_mean_x100"] == -9542
    assert windows["radio"][0]["snr_mean_x100"] == 0
    assert windows["pdr"][0]["pdr_x100"] == 7500
    assert windows["pdr"][0]["estimated_lost_count"] == 2
    assert all(rows[0]["duration_ms"] == 60000 and rows[0]["age_ms"] >= 0 for rows in windows.values())
    assert hub.broadcast.await_count == 0
