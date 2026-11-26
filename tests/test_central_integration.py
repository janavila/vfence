import asyncio
import json
import logging
import sqlite3
from pathlib import Path
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
        self.writes.append(data.decode("utf-8").strip())
        return len(data)

    def flush(self):
        return None


def parse_serial_command(line):
    assert line.startswith("[CMD] ")
    return json.loads(line[6:])


async def answer_serial_commands(reader, connection, *, stop_at):
    answered = 0
    commands = []
    for _ in range(300):
        while answered < len(connection.writes):
            command = parse_serial_command(connection.writes[answered])
            answered += 1
            commands.append(command)
            await reader.process_line("[APP] " + json.dumps({
                "schema": "vfence.gateway.serial.v1",
                "kind": "command_response",
                "requestId": command["requestId"],
                "command": command["command"],
                "ok": True,
            }))
            if command["command"] == stop_at:
                return commands
        await asyncio.sleep(0.005)
    raise AssertionError(f"command {stop_at} was not sent")


async def test_serial_fence_commands_are_json_ordered_and_wait_for_confirmation():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    reader = SerialReader(config, db, Hub())
    connection = FakeConnection()
    reader._connection = connection

    task = asyncio.create_task(reader.send_fence("COL01", contract_fence()))
    commands = await answer_serial_commands(reader, connection, stop_at="FENCE_SEND")
    assert [item["command"] for item in commands] == [
        "SET_PARAMETERS", "FENCE_NEW",
        "FENCE_POINT", "FENCE_POINT", "FENCE_POINT", "FENCE_POINT",
        "FENCE_SEND",
    ]
    assert [item["requestId"] for item in commands] == list(range(1, 8))
    assert commands[1]["collarId"] == 1
    assert commands[1]["fenceVersion"] == 1
    assert commands[2]["latitude"] == -31.306
    assert commands[2]["longitude"] == -54.0642
    assert not task.done()
    await reader.process_line('[APP] {"schema":"vfence.gateway.serial.v1",'
                              '"kind":"fence_transfer","requestId":7,'
                              '"collarId":1,"fenceVersion":1,"state":"started"}')
    assert not task.done()
    await reader.process_line('[APP] {"schema":"vfence.gateway.serial.v1",'
                              '"kind":"fence_transfer","requestId":7,'
                              '"collarId":1,"fenceVersion":1,"state":"confirmed"}')
    assert await task == (True, "collar_confirmed")


async def test_serial_fence_structured_failure_keeps_result_as_failure():
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    reader = SerialReader(config, db, Hub())
    connection = FakeConnection()
    reader._connection = connection
    task = asyncio.create_task(reader.send_fence("COL01", contract_fence()))
    commands = await answer_serial_commands(reader, connection, stop_at="FENCE_SEND")
    request_id = commands[-1]["requestId"]
    await reader.process_line("[APP] " + json.dumps({
        "schema": "vfence.gateway.serial.v1",
        "kind": "fence_transfer",
        "requestId": request_id,
        "collarId": 1,
        "fenceVersion": 1,
        "state": "failed",
        "detail": "CRC_ERROR",
    }))
    assert await task == (False, "CRC_ERROR")


async def test_serial_fence_confirmation_timeout_is_reported(caplog):
    config = base_config()
    db = Database("sqlite:///:memory:")
    db.initialize()
    reader = SerialReader(config, db, Hub())
    connection = FakeConnection()
    reader._connection = connection

    task = asyncio.create_task(
        reader.send_fence("COL01", contract_fence(), timeout_seconds=0.01)
    )
    commands = await answer_serial_commands(reader, connection, stop_at="FENCE_SEND")
    assert await task == (False, "gateway_confirmation_timeout")
    # A report arriving after timeout cannot resurrect the previous operation.
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        await reader.process_line("[APP] " + json.dumps({
            "schema": "vfence.gateway.serial.v1", "kind": "fence_transfer",
            "requestId": commands[-1]["requestId"], "collarId": 1, "fenceVersion": 1,
            "state": "failed", "detail": "ACK_TIMEOUT",
        }))
    assert "CERCA | RELATO SEM CORRELACAO" in caplog.text
    assert caplog.records[-1].levelno == logging.INFO
    assert reader._fence_result is None


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


async def test_windows_sync_recovers_network_and_lost_commit_against_central(tmp_path, monkeypatch, caplog):
    from app.fpga_service import FpgaService

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "web"))
    config = base_config(windows_base=True, known_collar_ids=("COL01",), base_token="isolated-central-test-key",
                         metrics_gateway_id="test-gateway", metrics_installation_id="test-site")
    for key, value in {"BASE_ID": config.base_id, "BASE_TOKEN": config.base_token,
                       "DATA_DIR": str(tmp_path / "central"), "COLARES_CONHECIDOS": "COL01",
                       "AUTH_ENABLED": "false", "DATABASE_URL": ""}.items():
        monkeypatch.setenv(key, value)
    from backend.config import load_settings
    from backend.main import create_app

    central = create_app(settings=load_settings(env_file=None), log_to_file=False)
    base = Database(f"sqlite:///{tmp_path / 'base.db'}")
    base.initialize()
    hub = Hub()
    reader = SerialReader(config, base, hub)
    reader.state.status = "online"
    fpga = FpgaService(config, base, reader)

    async def ingest(local_sequence):
        assert await reader.process_line('[APP] ' + json.dumps({
            "schema": "vfence.gateway.serial.v1", "kind": "telemetry", "collarId": 1,
            "sequence": local_sequence, "latitude": -31.3, "longitude": -54.0, "gpsValid": True,
            "inside": True, "rssi": -90, "snr": 7.5, "hdop": 0.9,
        }))

    class UnstableTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.app = httpx.ASGITransport(app=central)
            self.mode = "online"
            self.committed = None

        async def handle_async_request(self, request):
            assert request.headers["Authorization"] == "Bearer " + config.base_token
            if self.mode == "offline":
                raise httpx.ConnectError("network unavailable", request=request)
            if self.mode == "unavailable":
                return httpx.Response(503)
            if self.mode == "invalid_token":
                request.headers["Authorization"] = "Bearer invalid-test-token"
            response = await self.app.handle_async_request(request)
            if request.url.path == "/api/edge/batch":
                if self.mode == "lost_ack":
                    assert response.status_code == 200
                    self.committed = json.loads(request.content)
                    await response.aclose()
                    raise httpx.ReadError("response lost after remote COMMIT", request=request)
                if self.committed is not None:
                    attempted = json.loads(request.content)
                    assert attempted["generation"] == self.committed["generation"]
                    assert attempted["items"][:len(self.committed["items"])] == self.committed["items"]
                    self.committed = None
            return response

    async with central.router.lifespan_context(central):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=central), base_url="http://central.test") as producer:
            published = await producer.post("/api/fences", json={
                "name": "Teste", "margin_attention_m": 8, "margin_critical_m": 5,
                "points": [{"lat": lat, "lon": lon} for lat, lon in (
                    (-31.3058, -54.0646), (-31.3058, -54.0633),
                    (-31.3066, -54.0633), (-31.3066, -54.0646),
                )],
            })
            assert published.status_code == 201
            transport = UnstableTransport()
            serial = FakeSerial()
            sync = CentralSyncService(config, base, serial, hub, CentralClient(
                config.central_base_url, config.base_token, 1, transport))
            assert sync.as_dict()["phase"] == "connecting"
            await ingest(1)
            await sync._run_sync_iteration()
            assert sync.as_dict()["phase"] == "synchronized" and serial.sent == [("COL01", 1)]
            deliveries = (await producer.get("/api/fences/1/deliveries")).json()
            assert deliveries[0]["status_label"] == "Confirmada"

            # Network failure, a distinct server failure, and a real routing/auth check.
            for mode, phase in (("offline", "network_unavailable"), ("unavailable", "server_unavailable"),
                                ("invalid_token", "awaiting_credential")):
                transport.mode = mode
                assert await sync._run_sync_iteration() > 0
                assert sync.as_dict()["phase"] == phase and sync.as_dict()["authenticated"] is False
            await ingest(2)
            assert await reader.process_line('[APP] {"schema":"vfence.gateway.serial.v1","kind":"event",'
                                             '"collarId":1,"subtype":"GPS_LOST","sequence":22}')
            # Serial metric commit and FPGA-facing data stay local during HTTP 401.
            evidence = dict(schema="vfence.gateway.evidence.v1", kind="metric_evidence", collarId=1,
                            gatewaySessionId="1" * 16, collarSessionId="2" * 16, windowId="0" * 15 + "1",
                            startedAtMs=60000, durationMs=60000, profileId=0, txFirst=0, txLast=10,
                            receivedCount=8, pdrX100=8000, radioCount=8, rssiX100=-9000, snrX100=750,
                            hdopCount=2, hdopX100=90)
            assert await reader.process_line(b"[APP] " + json.dumps(evidence).encode() + b"\n")
            assert fpga.records([1])[1]["pdr"][0]["pdr_x100"] == 8000
            assert base.get_collar("COL01")["last_sequence"] == 2 and base.outbox_size() > 0

            transport.mode = "lost_ack"
            await sync._run_sync_iteration()
            assert transport.committed and base.outbox_size() > 0
            transport.mode = "online"
            await sync._run_sync_iteration()
            assert transport.committed is None
            assert sync.as_dict()["phase"] == "reconnected" and sync.as_dict()["authenticated"] is True
            await sync._run_sync_iteration()
            assert base.outbox_size() == 0 and serial.sent == [("COL01", 1)]
            with central.state.db.connect() as connection:
                assert connection.execute("SELECT COUNT(*) AS total FROM telemetry").fetchone()["total"] == 2
                assert connection.execute("SELECT last_heartbeat FROM bases").fetchone()["last_heartbeat"]
                assert connection.execute("SELECT COUNT(*) AS total FROM events WHERE kind='gps_lost'").fetchone()["total"] == 1
    assert "isolated-central-test-key" not in caplog.text


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

    async def send_batch_confirmed(self, base_id, generation, items):
        self.batches.extend(items)
        return [item["edge_seq"] for item in items]


class FakeSerial:
    def __init__(self):
        self.state = SimpleNamespace(status="online")
        self.sent = []

    async def send_fence(self, collar_id, fence):
        self.sent.append((collar_id, fence["version"]))
        return True, "confirmed"


async def test_worker_retries_same_network_failure_without_restart_or_repeated_log(caplog):
    config = base_config()
    database = Database("sqlite:///:memory:")
    database.initialize()
    requests = 0
    backoff = []

    async def handle(request):
        nonlocal requests
        if request.url.path.endswith("/heartbeat"):
            requests += 1
            if requests <= 2:
                if requests == 2:
                    backoff.append(worker.next_retry_at)
                raise httpx.ConnectError("offline", request=request)
            backoff.append(worker.next_retry_at)
            return httpx.Response(200, json={"desired_fence_version": None, "server_time": "2026-10-09T12:00:00Z"})
        batch = json.loads(request.content)
        await worker.stop()
        return httpx.Response(200, json={"generation": batch["generation"],
                                          "acked_sequences": [item["edge_seq"] for item in batch["items"]]})

    worker = CentralSyncService(config, database, FakeSerial(), Hub(), client=CentralClient(
        config.central_base_url, config.base_token, 1, httpx.MockTransport(handle)))
    await asyncio.wait_for(worker.run(), 3)
    assert requests == 3 and worker.status == "online"
    assert backoff[0] < backoff[1] and worker.next_retry_at is None
    assert caplog.text.count("SEM CONEXAO COM A CENTRAL") == 1


async def test_sync_downloads_delivers_to_multiple_collars_and_clears_outbox(caplog):
    config = base_config(terminal_detail=True)
    db = Database("sqlite:///:memory:")
    db.initialize()
    serial = FakeSerial()
    central = FakeCentral()
    service = CentralSyncService(config, db, serial, Hub(), client=central)
    with caplog.at_level(logging.INFO, logger="central_sync"):
        await service.sync_once()
    state = db.fence_state()
    assert state["downloaded_version"] == 1
    assert state["confirmed_version"] == 1
    assert serial.sent == [("COL01", 1), ("COL02", 1)]
    assert db.outbox_size() == 0
    logs = caplog.text
    assert logs.index("PUBLICACAO DETECTADA") < logs.index("Download validado") < logs.index("SQLite: SALVA NA BASE")
    assert logs.index("SQLite: SALVA NA BASE") < logs.index("CONFIRMADA PARA AS COLEIRAS CONFIGURADAS")
    assert logs.index("ACK de lote recebido") < logs.index("LOTE CONFIRMADO PELA CENTRAL") < logs.index("Outbox: ATUALIZADA")
    assert "ATIVADA" not in logs


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


@pytest.mark.parametrize(("failure", "status"), [
    ("FENCE_COMMIT rejeitado", "FALHA REGISTRADA NA BASE"),
    ("gateway_confirmation_timeout", "CONFIRMACAO NAO RECEBIDA"),
])
async def test_failed_new_fence_preserves_previous_confirmed_version(caplog, failure, status):
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
            return False, failure

    service = CentralSyncService(
        config, db, FailingSerial(), Hub(), client=CentralV2()
    )
    await service.sync_once()
    state = db.fence_state()
    assert state["downloaded_version"] == 2
    assert state["pending_version"] == 2
    assert state["confirmed_version"] == 1
    assert failure in state["last_error"] and status in caplog.text
    assert "[BASE][COL 001] CERCA | VERSAO 2" in caplog.text


@pytest.mark.parametrize(("error_type", "reason"), [
    (CentralAuthenticationError, "AUTENTICACAO RECUSADA"),
    (CentralClientError, "ERRO CENTRAL"),
])
async def test_central_failure_log_redacts_response_and_preserves_pending_outbox(caplog, error_type, reason):
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.enqueue_event("test", timestamp="2026-10-09T00:00:00Z")
    service = CentralSyncService(base_config(), db, FakeSerial(), Hub(), client=FakeCentral())

    async def fail():
        await service.stop()
        raise error_type("simulated sensitive response")

    service.sync_once = fail
    await service.run()
    assert "CICLO INDISPONIVEL" in caplog.text and reason in caplog.text
    assert "simulated sensitive response" not in caplog.text
    assert "LOTE CONFIRMADO PELA CENTRAL" not in caplog.text
    assert db.outbox_size() == 2


@pytest.mark.parametrize(("scenario", "reason", "technical"), [
    ("timeout", "TIMEOUT", "CentralClientError | Causa: ReadTimeout"),
    ("connection", "FALHA DE CONEXAO", "CentralClientError | Causa: ConnectError"),
    (429, "ERRO HTTP | HTTP 429", "CentralClientError | Causa: HTTPStatusError"),
    (503, "ERRO HTTP | HTTP 503", "CentralClientError | Causa: HTTPStatusError"),
    (401, "AUTENTICACAO RECUSADA | HTTP 401", "CentralAuthenticationError"),
    (403, "AUTENTICACAO RECUSADA | HTTP 403", "CentralAuthenticationError"),
    ("heartbeat", "ERRO DE VALIDACAO", "CentralClientError"),
    ("fence", "ERRO DE VALIDACAO", "InvalidFenceError"),
])
@pytest.mark.parametrize("detail", [False, True])
async def test_central_terminal_uses_safe_details_from_actual_client(caplog, scenario, reason, technical,
                                                              detail, monkeypatch):
    from app import main  # Loads the same logger configuration used by the Base.

    config = base_config(terminal_detail=detail, base_token="example-header-omit")
    db = Database("sqlite:///:memory:")
    db.initialize()
    hub = Hub()

    async def respond(request):
        assert request.url.params["diagnostic"] == "example-query-omit"
        assert request.headers["authorization"] == "Bearer example-header-omit"
        if scenario == "fence" and request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"desired_fence_version": 1, "server_time": "test"})
        await service.stop()
        if scenario == "timeout":
            raise httpx.ReadTimeout("example-timeout-omit", request=request)
        if scenario == "connection":
            raise httpx.ConnectError("example-connection-omit", request=request)
        if scenario == "fence":
            return httpx.Response(200, json={**contract_fence(), "crc32": "example-crc-omit"})
        if scenario == "heartbeat":
            return httpx.Response(200, json={"password": "example-json-omit"})
        return httpx.Response(scenario, text="example-body-omit", headers={"Set-Cookie": "example-cookie-omit"})

    client = CentralClient(config.central_base_url, config.base_token, 1, httpx.MockTransport(respond))
    request = client._request

    async def request_with_query(method, path, **kwargs):
        return await request(method, path, params={"diagnostic": "example-query-omit"}, **kwargs)

    monkeypatch.setattr(client, "_request", request_with_query)
    service = CentralSyncService(config, db, FakeSerial(), hub, client=client)
    with caplog.at_level(logging.INFO):
        await service.run()
    assert f"Motivo: {reason}" in caplog.text
    assert (f"Tipo: {technical}" in caplog.text) is detail
    assert "example-" not in caplog.text and "example-header-omit" not in caplog.text
    assert "EXAMPLE-CRC-OMIT" not in caplog.text and config.central_base_url not in caplog.text
    assert service.status == "offline" and service.last_error
    assert hub.messages[-1]["data"]["last_error"] == service.last_error


async def test_central_sqlite_failure_is_reported_and_retry_recovers(caplog, monkeypatch):
    config = base_config(terminal_detail=True)
    db = Database("sqlite:///:memory:")
    db.initialize()
    service = CentralSyncService(config, db, FakeSerial(), Hub(), client=FakeCentral())
    original = db.fence_state
    seen = 0

    def unavailable():
        nonlocal seen
        seen += 1
        if seen == 1:
            with sqlite3.connect(":memory:") as connection:
                connection.execute("SELECT * FROM example_private_table")
        return original()

    monkeypatch.setattr(db, "fence_state", unavailable)
    run = service._set_online

    async def stop_after_recovery():
        await run()
        await service.stop()

    monkeypatch.setattr(service, "_set_online", stop_after_recovery)
    await asyncio.wait_for(service.run(), 2)
    assert "Motivo: FALHA SQLITE" in caplog.text and "Tipo: OperationalError | Codigo SQLite: 1" in caplog.text
    assert "example_private_table" not in caplog.text
    assert service.status == "online" and service.last_error is None and seen == 2
    assert any(message["data"].get("status") == "storage_unavailable" for message in service.hub.messages
               if message["type"] == "central")


async def test_fence_resume_skips_only_recipients_with_confirmed_version_and_crc(tmp_path):
    path = tmp_path / "base.db"
    db = Database(f"sqlite:///{path}")
    db.initialize()
    fence = contract_fence()
    db.enqueue_delivery("COL01", fence["version"], "confirmed", "2026-10-09T00:00:00Z",
                        crc32=fence["crc32"])
    reopened = Database(f"sqlite:///{path}")
    reopened.initialize()
    serial = FakeSerial()
    service = CentralSyncService(base_config(), reopened, serial, Hub(), client=FakeCentral())
    await service._deliver_fence(fence)
    assert serial.sent == [("COL02", 1)]
    assert reopened.confirmed_recipients(1, fence["crc32"]) == {"COL01", "COL02"}


@pytest.mark.parametrize("reply", [
    {"acked_up_to": 999},
    {"generation": "different", "acked_sequences": [1]},
    {"acked_sequences": [True]},
    {"acked_sequences": [1, 2]},
    {"acked_sequences": []},
])
async def test_bad_commit_response_preserves_outbox(reply):
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.enqueue_event("outside", collar_id="COL01", timestamp="2026-10-09T00:00:00Z")
    generation = db.outbox_generation()
    config = base_config()

    def respond(request):
        assert json.loads(request.content)["generation"] == generation
        return httpx.Response(200, json={"generation": generation, **reply})

    client = CentralClient(config.central_base_url, "token", 1, httpx.MockTransport(respond))
    service = CentralSyncService(config, db, FakeSerial(), Hub(), client=client)
    with pytest.raises(CentralClientError, match="confirmação de lote incompatível"):
        await service._flush_outbox()
    assert [item["edge_seq"] for item in db.outbox_batch()] == [1]


def test_outbox_generation_survives_reopen_and_prevents_ack_across_recreation(tmp_path):
    path = tmp_path / "base.db"
    db = Database(f"sqlite:///{path}")
    db.initialize()
    generation = db.outbox_generation()
    reopened = Database(f"sqlite:///{path}")
    reopened.initialize()
    assert reopened.outbox_generation() == generation
    other = Database(f"sqlite:///{tmp_path / 'replacement.db'}")
    other.initialize()
    assert other.outbox_generation() != generation
    other.enqueue_event("outside", timestamp="2026-10-09T00:00:00Z")
    with pytest.raises(ValueError, match="generation changed"):
        other.acknowledge_confirmed_batch(generation, [1])
    assert other.outbox_size() == 1
