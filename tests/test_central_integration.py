import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.central_client import (
    CentralAuthenticationError,
    CentralClient,
    CentralClientError,
    InvalidFenceError,
    central_fence_crc32,
    validate_central_fence,
)
from app.database import Database
from app.serial_reader import SerialReader
from app.sync_service import CentralSyncService


def contract_fence():
    fence = {
        "version": 1,
        "margin_attention_cm": 500,
        "margin_critical_cm": 200,
        "points_e6": [
            [-31306000, -54064200],
            [-31306000, -54063700],
            [-31306400, -54063700],
            [-31306400, -54064200],
        ],
    }
    fence["crc32"] = central_fence_crc32(fence)
    return fence


def base_config(**overrides):
    values = dict(
        mock_serial=False,
        serial_port="fake",
        central_sync_enabled=True,
        central_base_url="http://central.test",
        base_token="token",
        central_timeout_seconds=1,
        central_sync_interval_seconds=0.01,
        central_retry_seconds=0.01,
        central_batch_size=100,
        base_id="BASE01",
        known_collar_ids=("COL01", "COL02"),
        collar_telemetry_seconds=10,
        collar_gps_timeout_seconds=60,
        collar_low_battery_pct=20,
        collar_critical_battery_pct=5,
        max_line_length=512,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Hub:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


def test_central_crc_matches_official_contract_vector():
    assert central_fence_crc32(contract_fence()) == "DA29321E"


def test_validate_central_fence_rejects_bad_crc_and_coordinates():
    bad_crc = contract_fence()
    bad_crc["crc32"] = "00000000"
    with pytest.raises(InvalidFenceError):
        validate_central_fence(bad_crc)
    bad_point = contract_fence()
    bad_point["points_e6"][0][0] = 91_000_000
    bad_point["crc32"] = central_fence_crc32(bad_point)
    with pytest.raises(InvalidFenceError):
        validate_central_fence(bad_point)


async def test_client_heartbeat_download_and_batch():
    fence = contract_fence()

    def handler(request):
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={
                "desired_fence_version": 1, "server_time": "2026-10-09T00:00:00Z"
            })
        if request.url.path.endswith("/fences/1"):
            return httpx.Response(200, json=fence)
        return httpx.Response(200, json={"acked_up_to": 7})

    client = CentralClient("http://central.test", "token", 1,
                           transport=httpx.MockTransport(handler))
    heartbeat = await client.heartbeat(
        base_id="BASE01", fence_version=None, serial_status="online", queue_size=0
    )
    assert heartbeat["desired_fence_version"] == 1
    assert (await client.download_fence(1))["crc32"] == "DA29321E"
    assert await client.send_batch("BASE01", [
        {"edge_seq": 7, "type": "event", "data": {"kind": "test"}}
    ]) == 7


async def test_client_classifies_authentication_and_network_errors():
    auth = CentralClient(
        "http://central.test", "bad", 1,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="no")),
    )
    with pytest.raises(CentralAuthenticationError):
        await auth.heartbeat(base_id="BASE01", fence_version=None,
                             serial_status="offline", queue_size=0)

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    offline = CentralClient(
        "http://central.test", "token", 1,
        transport=httpx.MockTransport(unavailable),
    )
    with pytest.raises(CentralClientError):
        await offline.heartbeat(base_id="BASE01", fence_version=None,
                                serial_status="offline", queue_size=0)


def test_outbox_survives_failure_until_acknowledged():
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.enqueue_event("central_offline", timestamp="2026-10-09T00:00:00+00:00")
    assert db.outbox_size() == 1
    item = db.outbox_batch()[0]
    assert item["type"] == "event"
    db.acknowledge_outbox(item["edge_seq"])
    assert db.outbox_size() == 0


class FakeConnection:
    is_open = True

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data.decode("ascii").strip())
        return len(data)

    def flush(self):
        return None


async def test_serial_fence_commands_are_ordered_and_wait_for_real_success_line():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    reader = SerialReader(config, db, Hub())
    connection = FakeConnection()
    reader._connection = connection

    task = asyncio.create_task(reader.send_fence("COL01", contract_fence()))
    for _ in range(100):
        if connection.writes and connection.writes[-1] == "FENCE_SEND":
            break
        await asyncio.sleep(0.01)
    assert connection.writes[0].startswith("PARAM 1 ")
    assert connection.writes[1] == "FENCE_NEW 1 1 4"
    assert connection.writes[2].startswith("FENCE_POINT 0 -31.3060000 -54.0642000")
    assert connection.writes[-1] == "FENCE_SEND"
    assert not task.done()
    await reader.process_line("[FENCE TX] SUCESSO: nova cerca confirmada pela coleira.")
    assert await task == (True, "collar_confirmed")


async def test_serial_fence_nack_keeps_result_as_failure():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    reader = SerialReader(config, db, Hub())
    reader._connection = FakeConnection()
    task = asyncio.create_task(reader.send_fence("COL01", contract_fence()))
    for _ in range(100):
        if reader._connection.writes and reader._connection.writes[-1] == "FENCE_SEND":
            break
        await asyncio.sleep(0.01)
    await reader.process_line(
        "[FENCE TX] transferencia abortada: FENCE_COMMIT rejeitado"
    )
    assert await task == (False, "FENCE_COMMIT rejeitado")


async def test_gateway_app_telemetry_enters_database_and_outbox():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    hub = Hub()
    reader = SerialReader(config, db, hub)
    await reader.process_line(
        '[APP] {"type":"telemetry","collarId":2,"sequence":9,'
        '"latitude":-31.3,"longitude":-54.0,"battery":88,'
        '"satellites":10,"hdop":0.9,"inside":true,"near":false,'
        '"veryNear":false,"gpsValid":true,"fenceVersion":1,'
        '"rssi":-90,"snr":7.5}'
    )
    assert db.get_collar("COL02")["zone"] == "SEGURO"
    assert db.outbox_size() >= 1
    assert hub.messages[-1]["type"] == "telemetry"


class FakeCentral:
    def __init__(self):
        self.fence = contract_fence()
        self.batches = []
        self.downloads = 0

    async def heartbeat(self, **kwargs):
        return {"desired_fence_version": 1, "server_time": "2026-10-09T00:00:00Z"}

    async def download_fence(self, version):
        assert version == 1
        self.downloads += 1
        return self.fence

    async def send_batch(self, base_id, items):
        self.batches.extend(items)
        return max(item["edge_seq"] for item in items)


class FakeSerial:
    def __init__(self):
        self.state = SimpleNamespace(status="online")
        self.sent = []

    async def send_fence(self, collar_id, fence):
        self.sent.append((collar_id, fence["version"]))
        return True, "confirmed"


async def test_sync_downloads_delivers_to_multiple_collars_and_clears_outbox():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    serial = FakeSerial()
    central = FakeCentral()
    service = CentralSyncService(config, db, serial, Hub(), client=central)
    await service.sync_once()
    state = db.fence_state()
    assert state["downloaded_version"] == 1
    assert state["confirmed_version"] == 1
    assert serial.sent == [("COL01", 1), ("COL02", 1)]
    assert db.outbox_size() == 0


async def test_same_local_version_is_not_downloaded_or_delivered_again():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.save_pending_fence(contract_fence(), "2026-10-09T00:00:00+00:00")
    db.set_fence_progress(
        sent_version=1, confirmed_version=1,
        timestamp="2026-10-09T00:00:01+00:00",
    )
    serial = FakeSerial()
    central = FakeCentral()
    await CentralSyncService(config, db, serial, Hub(), client=central).sync_once()
    assert central.downloads == 0
    assert serial.sent == []


def test_fence_snapshot_converts_e6_to_degrees_and_survives_restart(tmp_path):
    path = tmp_path / "vfence.db"
    db = Database(f"sqlite:///{path}")
    db.initialize()
    db.save_pending_fence(contract_fence(), "2026-10-09T00:00:00+00:00")
    db.set_fence_progress(
        sent_version=1, confirmed_version=1,
        timestamp="2026-10-09T00:00:01+00:00",
    )

    reopened = Database(f"sqlite:///{path}")
    reopened.initialize()
    snapshot = reopened.fence_snapshot()
    assert snapshot["status"] == "synchronized"
    assert snapshot["point_count"] == 4
    assert snapshot["points"][0] == {
        "index": 0,
        "latitude_e6": -31306000,
        "longitude_e6": -54064200,
        "latitude": -31.306,
        "longitude": -54.0642,
    }


async def test_mock_serial_confirms_fence_without_gateway():
    config = base_config(mock_serial=True, known_collar_ids=("COL01",))
    db = Database("sqlite:///:memory:")
    db.initialize()
    serial = SerialReader(config, db, Hub())
    service = CentralSyncService(config, db, serial, Hub(), client=FakeCentral())
    await service.sync_once()
    assert db.fence_snapshot()["status"] == "synchronized"


async def test_central_offline_keeps_last_local_fence_available():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.save_pending_fence(contract_fence(), "2026-10-09T00:00:00+00:00")
    db.set_fence_progress(
        sent_version=1, confirmed_version=1,
        timestamp="2026-10-09T00:00:01+00:00",
    )

    class OfflineCentral(FakeCentral):
        async def heartbeat(self, **kwargs):
            raise CentralClientError("central offline")

    service = CentralSyncService(
        config, db, FakeSerial(), Hub(), client=OfflineCentral()
    )
    with pytest.raises(CentralClientError):
        await service.sync_once()
    assert db.fence_snapshot()["downloaded_version"] == 1
    assert db.fence_snapshot()["point_count"] == 4


async def test_invalid_new_fence_does_not_replace_valid_local_fence():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.save_pending_fence(contract_fence(), "2026-10-09T00:00:00+00:00")
    db.set_fence_progress(
        sent_version=1, confirmed_version=1,
        timestamp="2026-10-09T00:00:01+00:00",
    )

    class InvalidCentral(FakeCentral):
        async def heartbeat(self, **kwargs):
            return {"desired_fence_version": 2, "server_time": "2026-10-09T00:00:00Z"}

        async def download_fence(self, version):
            raise InvalidFenceError("CRC Central inválido")

    service = CentralSyncService(
        config, db, FakeSerial(), Hub(), client=InvalidCentral()
    )
    with pytest.raises(InvalidFenceError):
        await service.sync_once()
    snapshot = db.fence_snapshot()
    assert snapshot["downloaded_version"] == 1
    assert snapshot["confirmed_version"] == 1
    assert snapshot["point_count"] == 4


async def test_failed_new_fence_preserves_previous_confirmed_version():
    config = base_config(known_collar_ids=("COL01",))
    db = Database("sqlite:///:memory:")
    db.initialize()
    old = contract_fence()
    db.save_pending_fence(old, "2026-10-09T00:00:00+00:00")
    db.set_fence_progress(
        sent_version=1, confirmed_version=1,
        timestamp="2026-10-09T00:00:01+00:00",
    )

    new = contract_fence()
    new["version"] = 2
    new["crc32"] = central_fence_crc32(new)

    class CentralV2(FakeCentral):
        def __init__(self):
            super().__init__()
            self.fence = new

        async def heartbeat(self, **kwargs):
            return {"desired_fence_version": 2, "server_time": "2026-10-09T00:00:00Z"}

        async def download_fence(self, version):
            assert version == 2
            return self.fence

    class FailingSerial(FakeSerial):
        async def send_fence(self, collar_id, fence):
            self.sent.append((collar_id, fence["version"]))
            return False, "FENCE_COMMIT rejeitado"

    service = CentralSyncService(
        config, db, FailingSerial(), Hub(), client=CentralV2()
    )
    await service.sync_once()
    state = db.fence_state()
    assert state["downloaded_version"] == 2
    assert state["pending_version"] == 2
    assert state["confirmed_version"] == 1
    assert "FENCE_COMMIT rejeitado" in state["last_error"]
