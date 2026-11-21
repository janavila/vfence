import asyncio
import json
import logging
import sqlite3
import tempfile
import threading
import unittest
from collections import deque
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database, MetricSaveResult, MetricSummary
from app.protocol import parse_message
from app.serial_reader import (
    GATEWAY_SERIAL_SCHEMA,
    SerialReader,
    _CRITICAL_EVENT_CAPACITY,
    _METRIC_QUEUE_CAPACITY,
    _SERIAL_READ_BYTES,
    _WORK_QUEUE_CAPACITY,
)


def hdop_summary(**fields):
    summary = MetricSummary(
        kind="HDOP", producer_id="INSTALL-TEST", collar_id=1,
        session_id="ffffffffffffffff", window_id="fffffffffffffffe",
        started_at_ms=4294967295, duration_ms=60000, sample_count=58,
        hdop_mean_x100=142, hdop_mean_valid=True,
        gateway_session_id="8000000000000000", gateway_received_at_ms=1250,
    )
    return replace(summary, **fields)


def radio_summary(**fields):
    summary = MetricSummary(
        kind="RADIO", producer_id="GATEWAY-TEST", collar_id=1,
        session_id="8000000000000000", window_id="fffffffffffffffe",
        started_at_ms=120000, duration_ms=60000, sample_count=8,
        rssi_mean_x100=-9340, snr_mean_x100=-375, rssi_mean_valid=True, snr_mean_valid=True,
    )
    return replace(summary, **fields)


def metric_rows(database):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM metric_summaries ORDER BY id")]


class MetricDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(f"sqlite:///{Path(directory.name) / 'test.db'}")
        self.database.initialize()

    def test_valid_empty_negative_and_zero_summaries(self):
        summaries = (
            hdop_summary(),
            hdop_summary(collar_id=2, sample_count=0, hdop_mean_x100=None, hdop_mean_valid=False),
            radio_summary(),
            radio_summary(collar_id=2, sample_count=0, rssi_mean_x100=None, snr_mean_x100=None,
                          rssi_mean_valid=False, snr_mean_valid=False),
            radio_summary(collar_id=254, snr_mean_x100=0),
            hdop_summary(collar_id=254, hdop_mean_x100=0),
        )
        for summary in summaries:
            self.assertIs(self.database.save_metric_summary(summary), MetricSaveResult.INSERTED)
        hdop, hdop_empty, radio, radio_empty, radio_zero, hdop_zero = metric_rows(self.database)
        self.assertEqual((hdop["session_id"], hdop["window_id"]), ("ffffffffffffffff", "fffffffffffffffe"))
        self.assertEqual(hdop["hdop_mean_x100"], 142)
        self.assertEqual((hdop_empty["hdop_mean_x100"], hdop_empty["hdop_mean_valid"]), (None, 0))
        self.assertEqual((radio["rssi_mean_x100"], radio["snr_mean_x100"]), (-9340, -375))
        self.assertEqual((radio_empty["rssi_mean_x100"], radio_empty["snr_mean_x100"]), (None, None))
        self.assertEqual((radio_zero["snr_mean_x100"], radio_zero["snr_mean_valid"]), (0, 1))
        self.assertEqual((hdop_zero["hdop_mean_x100"], hdop_zero["hdop_mean_valid"]), (0, 1))
        self.assertIn("+00:00", hdop["persisted_at"])

    def test_identity_duplicates_conflict_and_first_observation(self):
        original = hdop_summary()
        self.assertIs(self.database.save_metric_summary(original), MetricSaveResult.INSERTED)
        first = metric_rows(self.database)[0]
        self.assertIs(self.database.save_metric_summary(replace(original, gateway_received_at_ms=9999)),
                      MetricSaveResult.DUPLICATE)
        self.assertIs(self.database.save_metric_summary(replace(original, hdop_mean_x100=200)),
                      MetricSaveResult.CONFLICT)
        self.assertEqual(metric_rows(self.database), [first])
        for summary in (
            replace(original, collar_id=254),
            replace(original, session_id="0000000000000000"),
            replace(original, producer_id="INSTALL-OTHER"),
            radio_summary(),
            radio_summary(producer_id="GATEWAY-OTHER"),
        ):
            self.assertIs(self.database.save_metric_summary(summary), MetricSaveResult.INSERTED)
        self.assertEqual(len(metric_rows(self.database)), 6)

    def test_database_constraints_on_direct_writes(self):
        for summary in (
            hdop_summary(window_id="0000000000000000"),
            hdop_summary(gateway_received_at_ms=None),
            hdop_summary(hdop_mean_x100=None),
            hdop_summary(hdop_mean_x100=65535),
            radio_summary(rssi_mean_valid=False),
            radio_summary(snr_mean_x100=32768),
        ):
            self.assertIs(self.database.save_metric_summary(summary), MetricSaveResult.FAILED)
        self.assertEqual(metric_rows(self.database), [])

    def test_direct_writes_validate_before_sqlite_type_affinity(self):
        original = hdop_summary()
        self.assertIs(self.database.save_metric_summary(original), MetricSaveResult.INSERTED)
        before = metric_rows(self.database)
        for values in (
            {"session_id": "ffffffffffffffff\x00invalid"},
            {"window_id": "fffffffffffffffe\x00tail"},
            {"gateway_session_id": "8000000000000000\x00"},
            {"producer_id": " \t "}, {"producer_id": "INSTALL\x00A"},
            {"sample_count": "58"}, {"sample_count": True},
            {"hdop_mean_x100": 142.0}, {"duration_ms": 60000.0},
            {"hdop_mean_valid": 1}, {"started_at_ms": 1 << 70},
            {"collar_id": True}, {"gateway_received_at_ms": False},
        ):
            with self.subTest(values=values):
                self.assertIs(self.database.save_metric_summary(replace(original, **values)),
                              MetricSaveResult.FAILED)
        # A identidade tem a forma esperada, mas o driver recusa Unicode invalido.
        self.assertIs(self.database.save_metric_summary(replace(original, producer_id="\ud800")),
                      MetricSaveResult.FAILED)
        self.assertEqual(metric_rows(self.database), before)

    def test_commit_failure_rolls_back(self):
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE valid_ids (id INTEGER PRIMARY KEY);
                CREATE TABLE deferred_failure (
                    id INTEGER REFERENCES valid_ids(id) DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TRIGGER fail_at_commit AFTER INSERT ON metric_summaries
                    BEGIN INSERT INTO deferred_failure VALUES (NEW.id); END;
            """)
        self.assertIs(self.database.save_metric_summary(hdop_summary()), MetricSaveResult.FAILED)
        self.assertEqual(metric_rows(self.database), [])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM deferred_failure").fetchone()[0], 0)

    def test_instantaneous_storage_remains_independent(self):
        packet = parse_message("V1|COL01|POS|-31|-54|SEGURO|9|0.90|7")
        self.database.save_packet(packet)
        self.assertEqual(metric_rows(self.database), [])
        self.assertIs(self.database.save_metric_summary(hdop_summary()), MetricSaveResult.INSERTED)
        self.assertEqual(len(self.database.telemetry("COL01")), 1)
        self.assertEqual(self.database.get_collar("COL01")["hdop"], 0.90)
        self.assertEqual(len(self.database.events()), 1)
        self.assertEqual(self.database.outbox_size(), 0)


def hdop_json(**fields):
    return {
        "schema": GATEWAY_SERIAL_SCHEMA, "kind": "hdop_window_summary", "collarId": 1,
        "collarSessionId": "ffffffffffffffff", "windowId": "fffffffffffffffe",
        "collarStartedAtMs": 4294967295, "durationMs": 60000, "sampleCount": 58,
        "hdopMeanX100": 142, "hdopMeanValid": True,
        "gatewaySessionId": "8000000000000000", "gatewayReceivedAtMs": 1250, **fields,
    }


def radio_json(**fields):
    return {
        "schema": GATEWAY_SERIAL_SCHEMA, "kind": "radio_window_summary", "collarId": 1,
        "gatewaySessionId": "8000000000000000", "windowId": "fffffffffffffffe",
        "gatewayStartedAtMs": 120000, "durationMs": 60000, "sampleCount": 8,
        "rssiMeanX100": -9340, "snrMeanX100": -375,
        "rssiMeanValid": True, "snrMeanValid": True, **fields,
    }


def serial_line(data, terminator=b"\n"):
    return b"[APP] " + json.dumps(data, separators=(",", ":")).encode() + terminator


class RecordingHub:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


class FakeByteConnection:
    def __init__(self, *parts):
        self.parts = deque(parts)
        self.condition = threading.Condition()
        self.closed = False
        self.read_sizes = []
        self.writes = []
        self.is_open = True

    def feed(self, *parts):
        with self.condition:
            self.parts.extend(parts)
            self.condition.notify()

    @property
    def in_waiting(self):
        with self.condition:
            return len(self.parts[0]) if self.parts and isinstance(self.parts[0], bytes) else 0

    def read(self, size):
        with self.condition:
            self.read_sizes.append(size)
            self.condition.wait_for(lambda: self.parts or self.closed, timeout=0.02)
            if not self.parts:
                return b""
            part = self.parts.popleft()
            if isinstance(part, Exception):
                raise part
            if len(part) > size:
                self.parts.appendleft(part[size:])
            return part[:size]

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        with self.condition:
            self.closed = True
            self.is_open = False
            self.condition.notify()


class MetricSerialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(f"sqlite:///{Path(directory.name) / 'serial.db'}")
        self.database.initialize()
        self.hub = RecordingHub()
        self.config = SimpleNamespace(
            serial_port="test", serial_baud=115200, reconnect_seconds=0,
            mock_serial=False, central_sync_enabled=False, max_line_length=512,
            metrics_installation_id="INSTALL-TEST", metrics_gateway_id="GATEWAY-TEST",
        )
        self.reader = SerialReader(self.config, self.database, self.hub)

    async def wait_for(self, predicate):
        async def inspect():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(inspect(), 3)

    async def test_valid_empty_and_interleaved_summaries(self):
        payloads = (
            hdop_json(),
            radio_json(collarId=254, snrMeanX100=0),
            hdop_json(collarId=2, sampleCount=0, hdopMeanX100=None, hdopMeanValid=False),
            radio_json(),
            radio_json(collarId=2, sampleCount=0, rssiMeanX100=None, snrMeanX100=None,
                       rssiMeanValid=False, snrMeanValid=False),
        )
        payloads[2].pop("gatewaySessionId")
        payloads[2].pop("gatewayReceivedAtMs")
        with patch.object(self.reader, "send_command") as send, patch.object(self.reader, "send_gateway_command") as cmd:
            for payload in payloads:
                await self.reader.process_line(serial_line(payload, b"\r\n"))
            send.assert_not_called()
            cmd.assert_not_called()
        hdop, radio_zero, hdop_empty, radio, radio_empty = metric_rows(self.database)
        self.assertEqual(hdop["session_id"], "ffffffffffffffff")
        self.assertEqual(hdop["window_id"], "fffffffffffffffe")
        self.assertEqual(hdop["producer_id"], "INSTALL-TEST")
        self.assertEqual(hdop["gateway_session_id"], "8000000000000000")
        self.assertEqual((radio_zero["collar_id"], radio_zero["snr_mean_x100"]), (254, 0))
        self.assertIsNone(hdop_empty["hdop_mean_x100"])
        self.assertIsNone(hdop_empty["gateway_session_id"])
        self.assertEqual((radio["rssi_mean_x100"], radio["snr_mean_x100"]), (-9340, -375))
        self.assertEqual(radio["producer_id"], "GATEWAY-TEST")
        self.assertIsNone(radio_empty["rssi_mean_x100"])
        self.assertEqual(self.hub.messages, [])
        self.assertEqual(self.database.list_collars(), [])
        self.assertEqual(self.database.telemetry("COL01"), [])
        self.assertEqual(self.database.events(), [])
        self.assertEqual(self.database.outbox_size(), 0)

    async def test_duplicate_and_conflict(self):
        await self.reader.process_line(serial_line(hdop_json()))
        with self.assertLogs("serial_reader", logging.DEBUG) as logs:
            await self.reader.process_line(serial_line(hdop_json(gatewayReceivedAtMs=2000)))
            await self.reader.process_line(serial_line(hdop_json(hdopMeanX100=123)))
        self.assertIn("DUPLICATE | REGISTRO IDENTICO JA PERSISTIDO", logs.output[0])
        self.assertIn("CONFLICT | IDENTIDADE COM CONTEUDO DIVERGENTE", logs.output[1])
        self.assertEqual(len(metric_rows(self.database)), 1)
        self.assertEqual(metric_rows(self.database)[0]["gateway_received_at_ms"], 1250)

    async def test_required_extra_fields_and_strict_value_validation(self):
        missing = hdop_json()
        missing.pop("hdopMeanX100")
        unpaired = hdop_json()
        unpaired.pop("gatewayReceivedAtMs")
        for data in (
            missing, unpaired, hdop_json(schema="other"), hdop_json(schema=None),
            hdop_json(kind="unknown"), hdop_json(extra=0), hdop_json(collarId=0),
            hdop_json(collarId=255), hdop_json(collarId=True), hdop_json(collarId="1"),
            hdop_json(collarStartedAtMs=1.0), hdop_json(collarSessionId="FFFFFFFFFFFFFFFF"),
            hdop_json(windowId="0000000000000000"), hdop_json(windowId=2),
            hdop_json(windowId="1234"), hdop_json(durationMs=59999), hdop_json(sampleCount=61),
            hdop_json(gatewayReceivedAtMs=None), hdop_json(hdopMeanX100=65535),
            hdop_json(hdopMeanX100=float("nan")), hdop_json(hdopMeanX100=float("inf")),
            hdop_json(hdopMeanValid=1), hdop_json(hdopMeanX100=None),
            hdop_json(sampleCount=0, hdopMeanValid=False, hdopMeanX100=0),
            radio_json(snrMeanValid=False), radio_json(snrMeanX100=-32769),
            radio_json(rssiMeanX100=32701), radio_json(sampleCount=65536),
        ):
            with self.subTest(data=data), self.assertLogs("serial_reader", logging.WARNING):
                await self.reader.process_line(serial_line(data))
        self.assertEqual(metric_rows(self.database), [])

    async def test_duplicate_keys_cannot_route_a_summary_to_telemetry(self):
        original = serial_line(hdop_json())
        duplicate_collar = original.replace(b'"collarId":1', b'"collarId":1,"collarId":2')
        duplicate_kind = original.replace(b'"collarId":1', b'"collarId":1,"kind":"telemetry","sequence":7')
        duplicate_escaped = original.replace(b'"collarId":1', b'"collarId":1,"collar\\u0049d":2')
        for line in (duplicate_collar, duplicate_kind, duplicate_escaped):
            with self.assertLogs("serial_reader", logging.WARNING) as logs:
                await self.reader.process_line(line)
            self.assertIn("chave JSON duplicada", logs.output[0])
        self.assertEqual(metric_rows(self.database), [])
        self.assertEqual(self.database.telemetry("COL01"), [])

    async def test_line_lengths_and_terminators(self):
        payload = serial_line(hdop_json(), b"")
        with self.assertLogs("serial_reader", logging.WARNING):
            await self.reader.process_line(payload)
            await self.reader.process_line(b" " + payload + b"\n")
            await self.reader.process_line(payload + b"\r")
            await self.reader.process_line(payload + b"\n\n")
            await self.reader.process_line(payload + b" " * (512 - len(payload)) + b"\n")
        self.assertEqual(metric_rows(self.database), [])
        await self.reader.process_line(payload + b" " * (511 - len(payload)) + b"\n")
        self.assertEqual(len(metric_rows(self.database)), 1)

    async def test_fragmented_lines_and_discard_until_next_terminator(self):
        frame = serial_line(hdop_json(), b"\r\n")
        for chunk in (frame[:3], frame[3:-2], frame[-2:-1]):
            await self.reader._process_serial_chunk(chunk)
            await self.reader._process_serial_chunk(b"")
            self.assertEqual(metric_rows(self.database), [])
        await self.reader._process_serial_chunk(frame[-1:])
        self.assertEqual(len(metric_rows(self.database)), 1)
        await self.reader._process_serial_chunk(b"[APP] {")
        with self.assertLogs("serial_reader", logging.WARNING):
            await self.reader._process_serial_chunk(b" " * 4096)
        await self.reader._process_serial_chunk(b"")
        await self.reader._process_serial_chunk(serial_line(radio_json()))
        self.assertEqual(len(metric_rows(self.database)), 1)
        await self.reader._process_serial_chunk(serial_line(radio_json()))
        self.assertEqual(len(metric_rows(self.database)), 2)

    async def test_suffix_after_timeout_cannot_start_a_new_physical_line(self):
        frame = serial_line(hdop_json())
        await self.reader._process_serial_chunk(b"X" * 513)
        await self.reader._process_serial_chunk(b"")
        await self.reader._process_serial_chunk(frame)
        self.assertEqual(metric_rows(self.database), [])

        legacy = serial_line({
            "type": "telemetry",
            "collarId": 2, "sequence": 1, "hdop": 0.9,
        }, b"")
        await self.reader._process_serial_chunk(legacy)
        await self.reader._process_serial_chunk(b"")
        self.assertEqual(len(self.database.telemetry("COL02")), 1)
        await self.reader._process_serial_chunk(frame + frame)
        self.assertEqual(len(metric_rows(self.database)), 1)
        self.assertEqual(len(self.database.telemetry("COL02")), 1)

    async def test_limits_precede_json_and_legacy_has_room_for_long_lines(self):
        fill = "a" * 900
        summaries = (
            serial_line(hdop_json(extra=fill)),
            b'[APP] {"extra":"' + fill.encode() + b'","\\u006bind":"radio_window_summar\\u0079"}\n',
            serial_line(hdop_json(extra="x" * (66 * 1024))),
        )
        with patch("app.serial_reader.json.loads", wraps=json.loads) as decode:
            for frame in summaries:
                for start in range(0, len(frame), _SERIAL_READ_BYTES):
                    await self.reader._process_serial_chunk(frame[start:start + _SERIAL_READ_BYTES])
            decode.assert_not_called()
        self.assertEqual(metric_rows(self.database), [])
        await self.reader._process_serial_chunk(b'[APP] {"kind":"telemetry","bad":"\xff"}\n')
        legacy = serial_line({
            "schema": GATEWAY_SERIAL_SCHEMA, "kind": "telemetry", "collarId": 3, "sequence": 10,
            "unrelated": {"kind": "hdop_window_summary"},
        }, b"")
        legacy += b" " * (4095 - len(legacy)) + b"\n"
        await self.reader._process_serial_chunk(legacy)
        self.assertEqual(len(self.database.telemetry("COL03")), 1)
        await self.reader._process_serial_chunk(serial_line(hdop_json()) + serial_line(radio_json()))
        self.assertEqual(len(metric_rows(self.database)), 2)

    async def test_exact_wire_limits_with_both_terminators(self):
        body = serial_line(hdop_json(), b"")
        for terminator in (b"\n", b"\r\n"):
            with self.subTest(terminator=terminator):
                frame = body + b" " * (512 - len(body) - len(terminator)) + terminator
                with self.assertLogs("serial_reader", logging.WARNING):
                    await self.reader._process_serial_chunk(frame[:-len(terminator)] + b" " + terminator)
                await self.reader._process_serial_chunk(frame)
        self.assertEqual(len(metric_rows(self.database)), 1)

    async def test_reconnect_discards_unterminated_fragment(self):
        reader = self.reader
        incomplete = serial_line(hdop_json(), b"")
        finished = serial_line(radio_json())

        before = FakeByteConnection(incomplete, OSError("test disconnect"))
        after = FakeByteConnection(finished)
        original_dispatch = reader._process_metric_summary

        async def dispatch_and_stop(data):
            await original_dispatch(data)
            await reader.stop()

        with patch("serial.Serial", side_effect=(before, after)), \
                patch.object(reader, "_process_metric_summary", side_effect=dispatch_and_stop):
            await asyncio.wait_for(reader.run(), 3)
        self.assertTrue(before.closed and after.closed)
        self.assertEqual([row["kind"] for row in metric_rows(self.database)], ["RADIO"])
        self.assertEqual([message["data"]["status"] for message in self.hub.messages], ["online", "offline", "online"])
        self.assertLessEqual(max(before.read_sizes + after.read_sizes), _SERIAL_READ_BYTES)

    async def test_sqlite_failure_and_subsequent_resume_without_ack(self):
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TRIGGER block_metric BEFORE INSERT ON metric_summaries
                    BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END;
            """)
        with self.assertLogs("serial_reader", logging.WARNING) as logs, \
                patch.object(self.reader, "send_command") as send:
            await self.reader.process_line(serial_line(hdop_json()))
            send.assert_not_called()
        self.assertIn("FAILED | FALHA DE PERSISTENCIA", logs.output[0])
        self.assertEqual(metric_rows(self.database), [])
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER block_metric")
        await self.reader.process_line(serial_line(hdop_json()))
        self.assertEqual(len(metric_rows(self.database)), 1)

    async def test_command_and_fence_responses_pass_a_blocked_sqlite_and_pending_position(self):
        reader = self.reader
        connection = FakeByteConnection(serial_line(hdop_json()))
        saving = threading.Event()
        original_save = self.database.save_metric_summary

        def observe_save(summary):
            saving.set()
            return original_save(summary)

        with closing(sqlite3.connect(self.database.path, timeout=0)) as lock, \
                patch("serial.Serial", return_value=connection), \
                patch.object(self.database, "save_metric_summary", side_effect=observe_save):
            lock.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(reader.run())
            try:
                await self.wait_for(saving.is_set)
                command = asyncio.create_task(reader.send_gateway_command("GET_STATUS", timeout_seconds=1))
                self.addAsyncCleanup(self.cancel_pending, command)
                await self.wait_for(lambda: bool(connection.writes))
                request = json.loads(connection.writes[0].removeprefix(b"\x00\n")[6:])
                reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 91, 3, 7
                fence = reader._fence_result = asyncio.get_running_loop().create_future()
                connection.feed(
                    b"V1|COL02|POS|-31|-54|SEGURO|9|0.90|7\n" +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "command_response",
                                 "requestId": request["requestId"], "command": "GET_STATUS", "ok": True}) +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "fence_transfer",
                                 "requestId": 91, "collarId": 3, "fenceVersion": 7, "state": "confirmed"}) +
                    serial_line(radio_json(collarId=3)),
                )
                self.assertTrue((await command)["ok"])
                self.assertEqual(await asyncio.wait_for(fence, 1), (True, "collar_confirmed"))
                self.assertEqual(lock.execute("SELECT count(*) FROM metric_summaries").fetchone()[0], 0)
                self.assertEqual(reader.state.status, "online")
                await self.wait_for(lambda: reader._work_queue.qsize() == 2)
            finally:
                lock.rollback()
                await reader.stop()
                await asyncio.wait_for(task, 4)
        self.assertEqual([(r["kind"], r["collar_id"]) for r in metric_rows(self.database)],
                         [("HDOP", 1), ("RADIO", 3)])
        self.assertEqual(len(self.database.telemetry("COL02")), 1)
        self.assertEqual(len(connection.writes), 1)

    @staticmethod
    async def cancel_pending(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_metric_queue_is_bounded_refuses_excess_and_keeps_room_for_legacy(self):
        reader = self.reader
        connection = FakeByteConnection(serial_line(hdop_json()))
        saving, release = threading.Event(), threading.Event()
        original_save = self.database.save_metric_summary
        original_packet_save = self.database.save_packet
        save_order = []

        def slow_save(summary):
            saving.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the SQLite worker")
            save_order.append("metric")
            return original_save(summary)

        def observe_packet_save(*args):
            save_order.append("position")
            return original_packet_save(*args)

        with patch("serial.Serial", return_value=connection), \
                patch.object(self.database, "save_metric_summary", side_effect=slow_save), \
                patch.object(self.database, "save_packet", side_effect=observe_packet_save), \
                self.assertLogs("serial_reader", logging.WARNING) as logs:
            task = asyncio.create_task(reader.run())
            try:
                await self.wait_for(saving.is_set)
                connection.feed(b"".join(
                    serial_line(hdop_json(collarId=index)) for index in range(2, _METRIC_QUEUE_CAPACITY + 4)
                ) + b"V1|COL02|POS|-31|-54|SEGURO|9|0.90|7\n")
                await self.wait_for(lambda: reader._work_queue.qsize() == _METRIC_QUEUE_CAPACITY + 1)
                self.assertEqual(reader._work_queue.regular_capacity, _WORK_QUEUE_CAPACITY)
                self.assertEqual(reader._work_queue.maxsize, _WORK_QUEUE_CAPACITY + _CRITICAL_EVENT_CAPACITY)
                self.assertEqual(reader._queued_metrics, _METRIC_QUEUE_CAPACITY)
                self.assertEqual(reader._refused_metrics, 2)
            finally:
                release.set()
                await reader.stop()
                await asyncio.wait_for(task, 4)
        self.assertEqual(len(metric_rows(self.database)), _METRIC_QUEUE_CAPACITY + 1)
        self.assertEqual(len(self.database.telemetry("COL02")), 1)
        self.assertEqual(save_order[:3], ["metric", "position", "metric"])
        self.assertIn("2 total refused", "\n".join(logs.output))
        self.assertEqual(connection.writes, [])

    async def test_full_queue_refuses_129th_pending_position_and_keeps_reading_control(self):
        reader = self.reader
        connection = FakeByteConnection(serial_line(hdop_json()))
        metric_started = threading.Event()
        position_started, release_position = threading.Event(), threading.Event()
        save_metric = self.database.save_metric_summary
        save_packet = self.database.save_packet

        def observe_metric(summary):
            metric_started.set()
            return save_metric(summary)

        def hold_first_position(packet, enqueue_for_central):
            if packet.sequence == 1:
                position_started.set()
                if not release_position.wait(5):
                    raise RuntimeError("test did not release the position")
            return save_packet(packet, enqueue_for_central)

        def position(sequence):
            collar = (sequence - 1) % 3 + 1
            return f"V1|COL{collar:02d}|POS|-31|-54|SEGURO|9|0.90|{sequence}\n".encode()

        with closing(sqlite3.connect(self.database.path, timeout=0)) as lock, \
                patch("serial.Serial", return_value=connection), \
                patch.object(self.database, "save_metric_summary", side_effect=observe_metric), \
                patch.object(self.database, "save_packet", side_effect=hold_first_position), \
                self.assertLogs("serial_reader", logging.INFO) as logs:
            lock.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(reader.run())
            command = None
            try:
                await self.wait_for(metric_started.is_set)
                connection.feed(b"".join(
                    serial_line(hdop_json(collarId=(i % 3) + 1, windowId=f"{i:016x}")) +
                    position(i) for i in range(1, 65)
                ))
                await self.wait_for(lambda: reader._work_queue.qsize() == _WORK_QUEUE_CAPACITY)
                self.assertEqual(reader._queued_metrics, _METRIC_QUEUE_CAPACITY)
                self.assertEqual((reader._admitted_work, reader._processed_work), (129, 0))

                command = asyncio.create_task(reader.send_gateway_command("GET_STATUS", timeout_seconds=1))
                await self.wait_for(lambda: bool(connection.writes))
                request = json.loads(connection.writes[0].removeprefix(b"\x00\n")[6:])
                reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 94, 3, 7
                fence = reader._fence_result = asyncio.get_running_loop().create_future()
                connection.feed(
                    position(65) +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "command_response",
                                 "requestId": request["requestId"], "command": "GET_STATUS", "ok": True}) +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "fence_transfer",
                                 "requestId": 94, "collarId": 3, "fenceVersion": 7, "state": "confirmed"})
                )
                self.assertTrue((await command)["ok"])
                self.assertEqual(await asyncio.wait_for(fence, 1), (True, "collar_confirmed"))
                self.assertEqual(reader._refused_legacy, 1)
                self.assertEqual(reader._work_queue.qsize(), 128)
                self.assertEqual(lock.execute("SELECT count(*) FROM telemetry").fetchone()[0], 0)

                lock.rollback()
                await self.wait_for(position_started.is_set)
                self.assertEqual(reader._work_queue.qsize(), 127)
                connection.feed(position(66))
                await self.wait_for(lambda: reader._admitted_work == 130)
                release_position.set()
                await asyncio.wait_for(reader._work_queue.join(), 6)
            finally:
                lock.rollback()
                release_position.set()
                await reader.stop()
                await asyncio.wait_for(task, 5)
                if command is not None:
                    await self.cancel_pending(command)

        self.assertEqual((reader._admitted_work, reader._processed_work, reader._failed_work), (130, 130, 0))
        self.assertEqual((reader._refused_legacy, reader._refused_metrics, reader._queued_metrics), (1, 0, 0))
        positions = [m["data"] for m in self.hub.messages if m["type"] == "telemetry"]
        expected = list(range(1, 65)) + [66]
        self.assertEqual([p["sequence"] for p in positions], expected)
        self.assertEqual([p["collar_id"] for p in positions], [f"COL{(i - 1) % 3 + 1:02d}" for i in expected])
        self.assertEqual(len(metric_rows(self.database)), 65)
        self.assertEqual(len(connection.writes), 1)
        self.assertIn("admitted=130, processed=130, failed=0, discarded=0, refused=1", "\n".join(logs.output))

    async def test_idle_shutdown_joins_worker_and_reports_zero_work(self):
        connection = FakeByteConnection()
        with patch("serial.Serial", return_value=connection), self.assertLogs("serial_reader", logging.INFO) as logs:
            task = asyncio.create_task(self.reader.run())
            try:
                await self.wait_for(lambda: self.reader._connection is connection)
            finally:
                await self.reader.stop()
                await asyncio.wait_for(task, 3)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.writes, [])
        self.assertIn("admitted=0, processed=0, failed=0, discarded=0, refused=0", "\n".join(logs.output))
        self.assertFalse(any(t.get_name() == "vfence-serial-storage" for t in asyncio.all_tasks()))

    async def test_worker_reports_sqlite_and_unexpected_errors_and_continues_reading(self):
        reader = self.reader
        with self.database.connect() as db:
            db.executescript("""
                CREATE TRIGGER reject_first_collar BEFORE INSERT ON metric_summaries
                WHEN NEW.collar_id = 1
                BEGIN SELECT RAISE(ABORT, 'metric write failed'); END;
            """)
        original_save = self.database.save_metric_summary
        completed = threading.Event()

        def save(summary):
            if summary.collar_id == 2:
                raise RuntimeError("unexpected metric worker failure")
            result = original_save(summary)
            if summary.collar_id == 3:
                completed.set()
            return result

        connection = FakeByteConnection(
            serial_line(hdop_json()) + serial_line(hdop_json(collarId=2)) +
            b"V1|COL02|POS|-31|-54|SEGURO|9|0.90|7\n" + serial_line(radio_json(collarId=3))
        )
        with patch("serial.Serial", return_value=connection) as open_port, \
                patch.object(self.database, "save_metric_summary", side_effect=save), \
                self.assertLogs("serial_reader", logging.WARNING) as logs:
            task = asyncio.create_task(reader.run())
            try:
                await self.wait_for(completed.is_set)
                self.assertEqual(reader.state.status, "online")
            finally:
                await reader.stop()
                await asyncio.wait_for(task, 4)
        self.assertEqual(open_port.call_count, 1)
        self.assertEqual([row["collar_id"] for row in metric_rows(self.database)], [3])
        self.assertEqual(len(self.database.telemetry("COL02")), 1)
        self.assertIn("FAILED | FALHA DE PERSISTENCIA", "\n".join(logs.output))
        self.assertIn("unexpected metric worker failure", "\n".join(logs.output))
        self.assertEqual(connection.writes, [])

    async def test_shutdown_counts_unstarted_jobs_and_awaits_inflight_work_on_cancellation(self):
        reader = self.reader
        connection = FakeByteConnection(serial_line(hdop_json()))
        saving, release = threading.Event(), threading.Event()
        original_save = self.database.save_metric_summary

        def slow_save(summary):
            saving.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the SQLite worker")
            return original_save(summary)

        with patch("serial.Serial", return_value=connection), \
                patch.object(self.database, "save_metric_summary", side_effect=slow_save), \
                patch("app.serial_reader._WORK_DRAIN_SECONDS", 0.02), \
                self.assertLogs("serial_reader", logging.WARNING) as logs:
            task = asyncio.create_task(reader.run())
            try:
                await self.wait_for(saving.is_set)
                connection.feed(serial_line(hdop_json(collarId=2)) + serial_line(radio_json(collarId=3)))
                await self.wait_for(lambda: reader._queued_metrics == 2)
                await reader.stop()
                task.cancel()
                await self.wait_for(lambda: reader._queued_metrics == 0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                await reader.stop()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
        self.assertTrue(connection.closed)
        self.assertEqual([row["collar_id"] for row in metric_rows(self.database)], [1])
        self.assertEqual((reader._admitted_work, reader._processed_work, reader._failed_work), (3, 1, 0))
        self.assertIn("discarded on shutdown (2 items, 2 metrics)", "\n".join(logs.output))
        self.assertFalse(any(t.get_name() == "vfence-serial-storage" for t in asyncio.all_tasks()))

    async def test_missing_producer_only_blocks_summaries_and_legacy_keeps_working(self):
        self.config.metrics_installation_id = ""
        self.config.metrics_gateway_id = ""
        with self.assertLogs("serial_reader", logging.WARNING) as logs:
            await self.reader.process_line(serial_line(hdop_json()))
            await self.reader.process_line(serial_line(hdop_json()))
            await self.reader.process_line(serial_line(radio_json()))
        self.assertEqual(len(logs.output), 2)
        await self.reader._process_serial_chunk(b"V1|COL01|POS|-31|-54|SEGURO|9|0.90|7\n")
        packet = {
            "schema": GATEWAY_SERIAL_SCHEMA, "kind": "telemetry", "collarId": 2, "sequence": 8,
            "gpsValid": True, "inside": True, "hdop": 0.95,
        }
        await self.reader._process_serial_chunk(serial_line(packet, b""))
        await self.reader._process_serial_chunk(b"")
        await self.reader._process_serial_chunk(b"\n")
        await self.reader._process_serial_chunk(serial_line({**packet, "unused": "x" * 513}))
        await self.reader.process_line(serial_line({
            **{key: value for key, value in packet.items() if key not in {"schema", "kind"}}, "type": "telemetry",
        }))
        self.assertEqual(self.database.get_collar("COL01")["hdop"], 0.90)
        self.assertEqual(self.database.get_collar("COL02")["hdop"], 0.95)
        self.assertEqual(len(self.database.telemetry("COL02")), 3)
        self.assertEqual(metric_rows(self.database), [])
