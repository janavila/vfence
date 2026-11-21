import asyncio
import json
import shutil
import sqlite3
import subprocess
import threading
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.database import Database
from app.metric_protocol import parse_metric_summary
from app.serial_reader import SerialReader
from test_metric_summaries import (
    FakeByteConnection,
    RecordingHub,
    hdop_json,
    metric_rows,
    radio_json,
    serial_line,
)


class MetricConnection(FakeByteConnection):
    write_timeout = None


RESYNC = b"\x00\n"


async def until(predicate):
    async def check():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(check(), 4)


@pytest.fixture
def serial_setup(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'stage56.db'}")
    database.initialize()
    config = SimpleNamespace(
        mock_serial=False, serial_port="test", serial_baud=115200, reconnect_seconds=0,
        central_sync_enabled=False, max_line_length=512, metrics_serial_ack_enabled=True,
        metrics_installation_id="INSTALL-TEST", metrics_gateway_id="GATEWAY-TEST",
    )
    return database, config


@asynccontextmanager
async def running(reader, *ports):
    with patch("serial.Serial", side_effect=ports):
        task = asyncio.create_task(reader.run())
        try:
            yield
        finally:
            await reader.stop()
            await asyncio.wait_for(task, 5)


def ack(line):
    line = line.removeprefix(RESYNC)
    assert line.startswith(b"[CMD] ") and line.endswith(b"\n") and len(line) <= 512
    message = json.loads(line[6:])
    assert message["command"] == "METRIC_COMMITTED"
    return message


@pytest.fixture(scope="module")
def host_firmware(tmp_path_factory):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("compilador C++ de host indisponível")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("metric-cpp") / "metric_serial_export"
    subprocess.run([
        compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
        "-I", str(root / "gateway/VFenceGateway"), str(root / "tests/cpp/test_metric_serial_export.cpp"),
        "-o", str(binary),
    ], check=True)
    return str(binary)


def test_gateway_scheduler_and_commit_frame_parser(host_firmware):
    subprocess.run([host_firmware], check=True)


def test_gateway_frames_are_accepted_by_metric_protocol(host_firmware):
    frames = subprocess.check_output([host_firmware, "frames"]).splitlines(keepends=True)
    assert len(frames) == 6
    assert all(line.startswith(b"[APP] ") and line.endswith(b"\n") and len(line) <= 512 for line in frames)
    values = [parse_metric_summary(json.loads(line[6:]), "TEST") for line in frames]
    assert [row.collar_id for row in values] == [1, 254, 254, 1, 7, 7]
    assert all(row.window_id == "fffffffffffffffe" and row.started_at_ms == 0xFFFFFFFF for row in values)
    assert values[0].hdop_mean_x100 == 142 and values[0].session_id == "ffffffffffffffff"
    assert values[0].gateway_session_id == values[2].session_id == "8000000000000000"
    assert values[1].hdop_mean_x100 is None and values[1].hdop_mean_valid is False
    assert (values[2].rssi_mean_x100, values[2].snr_mean_x100) == (-9340, -375)
    assert values[3].rssi_mean_x100 is values[3].snr_mean_x100 is None
    assert values[4].rssi_mean_x100 == values[4].snr_mean_x100 == values[5].hdop_mean_x100 == 0
    assert values[4].snr_mean_valid is values[4].rssi_mean_valid is values[5].hdop_mean_valid is True


async def test_lost_ack_gateway_retry_and_raspberry_restart(host_firmware, serial_setup):
    db, config = serial_setup
    gateway = await asyncio.create_subprocess_exec(
        host_firmware, "exchange", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        initial = await asyncio.wait_for(gateway.stdout.readline(), 4)
        repeated = await asyncio.wait_for(gateway.stdout.readline(), 4)
        assert initial and initial == repeated
        first_usb = MetricConnection(initial[:40], initial[40:])
        async with running(SerialReader(config, db, RecordingHub()), first_usb):
            await until(lambda: len(first_usb.writes) == 1)
            assert first_usb.writes[0].startswith(RESYNC)
            first = ack(first_usb.writes[0])
            before = metric_rows(db)
            assert len(before) == 1
            assert before[0]["hdop_mean_x100"] == 142
            assert first_usb.write_timeout is None
        # A primeira confirmação se perdeu, e a Base reabre o mesmo arquivo SQLite.
        reopened = Database(f"sqlite:///{db.path}")
        second_usb = MetricConnection(repeated)
        async with running(SerialReader(config, reopened, RecordingHub()), second_usb):
            await until(lambda: len(second_usb.writes) == 1)
            assert second_usb.writes[0].startswith(RESYNC)
            second = ack(second_usb.writes[0])
            assert second == first
            assert second["sessionId"] == "ffffffffffffffff"
            assert second["gatewaySessionId"] == "8000000000000000"
            assert metric_rows(reopened) == before
        _, errors = await asyncio.wait_for(gateway.communicate(second_usb.writes[0]), 4)
        assert gateway.returncode == 0, errors.decode()
    finally:
        if gateway.returncode is None:
            gateway.kill()
            await gateway.wait()


async def test_only_durable_and_provisioned_summaries_are_confirmed(serial_setup):
    db, config = serial_setup
    config.metrics_installation_id = ""
    connection = MetricConnection(serial_line(hdop_json()), serial_line(radio_json()), serial_line(radio_json()),
                                  serial_line(radio_json(rssiMeanX100=-5)),
                                  serial_line(radio_json(collarId=0)))
    with db.connect() as sql:
        sql.executescript("""
            CREATE TABLE stage56_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE stage56_fk (
                id INTEGER REFERENCES stage56_parent(id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER stage56_failure AFTER INSERT ON metric_summaries WHEN NEW.collar_id = 2
                BEGIN INSERT INTO stage56_fk VALUES (NEW.id); END;
        """)
    connection.feed(serial_line(radio_json(collarId=2)), b"V1|COL03|POS|-31|-54|SEGURO|9|0.90|7\n")
    reader = SerialReader(config, db, RecordingHub())
    async with running(reader, connection):
        await until(lambda: reader._processed_work == 5 and len(connection.writes) == 2)
        assert reader._failed_work == 2  # CONFLICT e COMMIT recusado.
        assert all(ack(line)["kind"] == "radio_window_summary" for line in connection.writes)
        assert [ack(line)["requestId"] for line in connection.writes] == [1, 2]
        assert all(ack(line)["collarId"] == 1 for line in connection.writes)
        assert [row["collar_id"] for row in metric_rows(db)] == [1]
        assert len(db.telemetry("COL03")) == 1


async def test_refused_summary_and_blocked_sqlite_never_ack_early(serial_setup):
    db, config = serial_setup
    first = MetricConnection(serial_line(hdop_json()))
    started = threading.Event()
    save = db.save_metric_summary

    def observe_save(summary):
        started.set()
        return save(summary)

    reader = SerialReader(config, db, RecordingHub())
    with closing(sqlite3.connect(db.path)) as blocker, \
            patch.object(db, "save_metric_summary", side_effect=observe_save), \
            patch("app.serial_reader._METRIC_QUEUE_CAPACITY", 1):
        blocker.execute("BEGIN IMMEDIATE")
        async with running(reader, first):
            try:
                await until(started.is_set)
                first.feed(serial_line(radio_json(collarId=2)), serial_line(hdop_json(collarId=254)))
                await until(lambda: reader._refused_metrics == 1)
                assert not first.writes
                # Uma resposta de controle ainda é lida enquanto o COMMIT está pendente.
                status = asyncio.create_task(reader.send_gateway_command("GET_STATUS"))
                await until(lambda: len(first.writes) == 1)
                request = json.loads(first.writes[0].removeprefix(RESYNC)[6:])
                first.feed(serial_line({"schema": request["schema"], "kind": "command_response",
                                        "requestId": request["requestId"], "command": "GET_STATUS", "ok": True}))
                assert (await asyncio.wait_for(status, 1))["ok"] is True
                assert len(first.writes) == 1
            finally:
                blocker.rollback()
            await until(lambda: len(first.writes) == 3)
            assert [ack(line)["collarId"] for line in first.writes[1:]] == [1, 2]
            assert sorted(row["collar_id"] for row in metric_rows(db)) == [1, 2]


async def test_old_usb_work_is_not_confirmed_on_replacement_port(serial_setup):
    db, config = serial_setup
    old = MetricConnection(serial_line(hdop_json()))
    new = MetricConnection()
    entered = threading.Event()
    reader = SerialReader(config, db, RecordingHub())
    original = db.save_metric_summary

    def stalled(summary):
        entered.set()
        assert released.wait(4)
        return original(summary)

    released = threading.Event()
    with patch.object(db, "save_metric_summary", side_effect=stalled):
        async with running(reader, old, new):
            try:
                await until(entered.is_set)
                old.feed(OSError("USB disconnected"))
                await until(lambda: reader._connection is new)
            finally:
                released.set()
            await until(lambda: reader._processed_work == 1)
            assert metric_rows(db) and not old.writes and not new.writes
            new.feed(serial_line(hdop_json()))
            await until(lambda: len(new.writes) == 1)
            assert ack(new.writes[0])["collarId"] == 1 and len(metric_rows(db)) == 1


async def test_default_and_shutdown_discard_do_not_write_acks(serial_setup):
    db, config = serial_setup
    config.metrics_serial_ack_enabled = False
    disabled = MetricConnection(serial_line(hdop_json()))
    reader = SerialReader(config, db, RecordingHub())
    async with running(reader, disabled):
        await until(lambda: reader._processed_work == 1)
        assert reader._metric_acks is None and not disabled.writes

    config.metrics_serial_ack_enabled = True
    port = MetricConnection(serial_line(radio_json(collarId=2)))
    reader = SerialReader(config, db, RecordingHub())
    entered, released = threading.Event(), threading.Event()
    original = db.save_metric_summary

    def stalled(summary):
        entered.set()
        assert released.wait(4)
        return original(summary)

    with patch.object(db, "save_metric_summary", side_effect=stalled), \
            patch("app.serial_reader._WORK_DRAIN_SECONDS", 0.02):
        async with running(reader, port):
            try:
                await until(entered.is_set)
                port.feed(serial_line(hdop_json(collarId=3)))
                await until(lambda: reader._queued_metrics == 1)
                await reader.stop()
                await until(lambda: reader._work_queue is None and reader._queued_metrics == 0)
            finally:
                released.set()
    assert not port.writes
    assert sorted(row["collar_id"] for row in metric_rows(db)) == [1, 2]


async def test_ack_queue_is_bounded_and_partial_write_is_separated(serial_setup):
    db, config = serial_setup
    entered, release = threading.Event(), threading.Event()

    class BrokenWriteOnce(MetricConnection):
        def write(self, data):
            if not self.writes:
                assert self.write_timeout == 0.25
                self.writes.append(data[:30])
                entered.set()
                assert release.wait(4)
                return 30
            return super().write(data)

    connection = BrokenWriteOnce(serial_line(hdop_json()))
    reader = SerialReader(config, db, RecordingHub())
    with patch("app.serial_reader._METRIC_ACK_CAPACITY", 1):
        async with running(reader, connection):
            try:
                await until(entered.is_set)
                connection.feed(serial_line(hdop_json(collarId=7)), serial_line(hdop_json(collarId=254)))
                await until(lambda: reader._refused_metric_acks == 1)
                assert len(metric_rows(db)) == 3 and len(connection.writes) == 1
            finally:
                release.set()
            await until(lambda: len(connection.writes) == 2)
            assert connection.writes[1].startswith(RESYNC + b"[CMD] ")
            assert ack(connection.writes[1])["collarId"] == 7
            connection.feed(serial_line(hdop_json(collarId=254)))
            await until(lambda: len(connection.writes) == 3)
            assert ack(connection.writes[2])["collarId"] == 254
            assert len(metric_rows(db)) == 3 and connection.write_timeout is None


@pytest.mark.parametrize(("command", "fields"), [
    ("GET_STATUS", {}),
    ("GET_RADIO", {}),
    ("FENCE_NEW", {"collarId": 7, "fenceVersion": 2, "vertexCount": 3}),
])
async def test_first_command_after_reconnect_discards_previous_fragment(
        host_firmware, serial_setup, command, fields):
    db, config = serial_setup
    config.metrics_serial_ack_enabled = False

    class InterruptedPort(MetricConnection):
        def write(self, data):
            partial = data[:data.index(b'"command"')]
            super().write(partial)
            return len(partial)

    old, new = InterruptedPort(), MetricConnection()
    reader = SerialReader(config, db, RecordingHub())
    async with running(reader, old, new):
        await until(lambda: reader._connection is old)
        with pytest.raises(OSError, match="incomplete Serial command"):
            await reader.send_gateway_command("GET_STATUS", request_id=17)
        old.feed(OSError("USB disconnected"))
        await until(lambda: reader._connection is new)
        request = asyncio.create_task(reader.send_gateway_command(command, request_id=2, **fields))
        await until(lambda: len(new.writes) == 1)
        assert new.writes[0].startswith(RESYNC)
        result = subprocess.check_output([host_firmware, "commands"], input=old.writes[0] + new.writes[0])
        assert result == f"2 {command}\n".encode()
        # A validação estrutural continua rejeitando a concatenação se o separador se perder.
        result = subprocess.check_output(
            [host_firmware, "commands"], input=old.writes[0] + new.writes[0].removeprefix(RESYNC),
        )
        assert result == b"invalid\n"
        new.feed(serial_line({"schema": "vfence.gateway.serial.v1", "kind": "command_response",
                              "requestId": 2, "command": command, "ok": True}))
        assert (await asyncio.wait_for(request, 1))["requestId"] == 2
