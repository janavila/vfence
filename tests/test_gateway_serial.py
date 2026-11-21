import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.database import Database
from app.serial_reader import GATEWAY_SERIAL_SCHEMA, SerialReader


class Hub:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


class FakeConnection:
    is_open = True

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data.decode("utf-8").strip())
        return len(data)

    def flush(self):
        return None


def config(**overrides):
    values = {
        "mock_serial": False,
        "serial_port": "fake",
        "max_line_length": 512,
        "central_sync_enabled": True,
        "collar_telemetry_seconds": 10,
        "collar_gps_timeout_seconds": 60,
        "collar_low_battery_pct": 20,
        "collar_critical_battery_pct": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_reader(**overrides):
    database = Database("sqlite:///:memory:")
    database.initialize()
    hub = Hub()
    reader = SerialReader(config(**overrides), database, hub)
    reader._connection = FakeConnection()
    return reader, database, hub


def app_message(kind, **fields):
    return "[APP] " + json.dumps({
        "schema": GATEWAY_SERIAL_SCHEMA,
        "kind": kind,
        **fields,
    }, separators=(",", ":"))


def written_command(reader, index=-1):
    line = reader._connection.writes[index]
    assert line.startswith("[CMD] ")
    return json.loads(line[6:])


async def wait_for_writes(reader, count):
    for _ in range(100):
        if len(reader._connection.writes) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"expected {count} serial writes")


async def test_command_is_compact_json_and_response_is_correlated_by_request_id():
    reader, _, _ = make_reader()
    task = asyncio.create_task(reader.send_gateway_command("GET_STATUS"))
    await wait_for_writes(reader, 1)

    command = written_command(reader)
    assert command == {
        "schema": GATEWAY_SERIAL_SCHEMA,
        "requestId": 1,
        "command": "GET_STATUS",
    }
    assert " " not in reader._connection.writes[0][6:]

    await reader.process_line(app_message(
        "command_response", requestId=999, command="GET_STATUS", ok=True
    ))
    assert not task.done()
    await reader.process_line(app_message(
        "command_response", requestId=1, command="GET_STATUS", ok=True
    ))
    assert (await task)["ok"] is True


async def test_request_ids_are_increasing():
    reader, _, _ = make_reader()
    for expected, command_name in enumerate(("GET_STATUS", "GET_RADIO"), start=1):
        task = asyncio.create_task(reader.send_gateway_command(command_name))
        await wait_for_writes(reader, expected)
        command = written_command(reader)
        assert command["requestId"] == expected
        await reader.process_line(app_message(
            "command_response", requestId=expected,
            command=command_name, ok=True,
        ))
        await task


async def test_command_timeout_removes_pending_request():
    reader, _, _ = make_reader()
    with pytest.raises(asyncio.TimeoutError):
        await reader.send_gateway_command("GET_STATUS", timeout_seconds=0.01)
    assert reader._pending_commands == {}


async def test_structured_telemetry_is_saved_and_broadcast():
    reader, database, hub = make_reader()
    await reader.process_line(app_message(
        "telemetry", collarId=2, sequence=9,
        latitude=-31.3, longitude=-54.0, battery=88,
        satellites=10, hdop=0.9, inside=True, near=False,
        veryNear=False, gpsValid=True, fenceVersion=1,
        rssi=-90, snr=7.5,
    ))
    assert database.get_collar("COL02")["zone"] == "SEGURO"
    assert hub.messages[-1]["type"] == "telemetry"


async def test_event_is_queued_and_control_is_broadcast():
    reader, database, hub = make_reader()
    await reader.process_line(app_message(
        "event", subtype="FENCE_EXIT", collarId=1, sequence=10
    ))
    event_item = database.outbox_batch()[-1]
    assert event_item["type"] == "event"
    assert event_item["data"]["kind"] == "fence_exit"

    await reader.process_line(app_message(
        "control", subtype="PONG", collarId=1, token=1234
    ))
    assert hub.messages[-1]["type"] == "gateway_event"
    assert hub.messages[-1]["data"]["subtype"] == "PONG"


async def test_status_and_radio_messages_update_reader_snapshot():
    reader, _, _ = make_reader()
    await reader.process_line(app_message(
        "gateway_state", state="ONLINE", fenceTransferActive=False
    ))
    await reader.process_line(app_message(
        "collar_status", collarId=1, state="ONLINE",
        lastSeenS=2, rssi=-83, snr=8.5,
    ))
    await reader.process_line(app_message(
        "collar_state", collarId=1, state="OFFLINE"
    ))
    await reader.process_line(app_message(
        "radio_config", frequencyHz=915000000, spreadingFactor=7,
        bandwidthHz=125000, codingRateDenominator=5,
        preambleLength=8, syncWord=18, txPowerDbm=14, crcEnabled=True,
    ))
    assert reader.gateway_state["state"] == "ONLINE"
    assert reader.collar_status[1]["state"] == "OFFLINE"
    assert reader.radio_config["frequencyHz"] == 915000000


async def test_human_firmware_logs_are_ignored_without_protocol_warning(caplog):
    reader, database, hub = make_reader()
    with caplog.at_level(logging.DEBUG):
        await reader.process_line("[FENCE TX] SUCESSO: texto apenas humano")
        await reader.process_line("Radio LoRa inicializado")
    assert not any("packet rejected" in record.message for record in caplog.records)
    assert database.list_collars() == []
    assert hub.messages == []


async def test_legacy_app_telemetry_remains_accepted_during_transition():
    reader, database, _ = make_reader()
    await reader.process_line(
        '[APP] {"type":"telemetry","collarId":1,"sequence":3,'
        '"latitude":-31.3,"longitude":-54.0,"battery":90,'
        '"satellites":9,"hdop":1.0,"inside":false,"near":false,'
        '"veryNear":false,"gpsValid":true,"fenceVersion":1}'
    )
    assert database.get_collar("COL01")["zone"] == "FORA"
