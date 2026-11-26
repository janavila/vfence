import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.fpga_service import FpgaService, GatewayClock, NetworkWindow
from app.fpga_window import FpgaSample
from app.jtag_atlantic import IsolatedJtagAtlantic, JtagAtlantic, JtagOwnerAlive, parse_result, sample_command
from app.serial_reader import SerialReader


class ScriptedJtag:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.last_sent = False

    def command(self, command):
        expected, reply = self.steps.popleft()
        assert command == expected
        self.last_sent = True
        if isinstance(reply, Exception):
            raise reply
        return reply


def service(steps):
    config = SimpleNamespace(metrics_gateway_id="gw", metrics_installation_id="site")
    reader = SimpleNamespace(serial_generation=1)
    instance = FpgaService(config, None, reader)
    instance.link = ScriptedJtag(steps)
    return instance


def test_wire_preserves_sign_and_missing_flags():
    sample = FpgaSample(31, 2, 9800, -500, 105, None, 5)
    assert sample_command(sample) == "SAMPLE 31 2 9800 -500 105 0 5 7"
    assert parse_result("VFENCE|P1|3|1", 1)["critical"]
    with pytest.raises(ValueError):
        parse_result("VFENCE|P2|3|1", 1)


async def test_fpga_waits_for_old_owner_before_recovering():
    config = SimpleNamespace(windows_base=True, fpga_enabled=True, known_collar_ids=("COL01",),
                             hdop_clock_map="{}", gateway_clock_map="{}", metrics_gateway_id="gw")
    link = ScriptedJtag([("PING", "PONG"), ("RESULT", "ERR|NO_RESULT"),
                         ("CLOSE", "ERR|NO_WINDOW"), ("PING", "PONG")])
    link.close = lambda: None
    checks = []
    owner_closed = []
    owner = SimpleNamespace(is_alive=lambda: (checks.append(1) or len(checks) < 3),
                            join=lambda timeout: None, close=lambda: owner_closed.append(True))
    attempts = []

    def transport(_):
        attempts.append(len(checks))
        if len(attempts) == 1:
            raise JtagOwnerAlive("old owner alive", owner=owner)
        assert owner_closed == [True]
        return link

    instance = FpgaService(config, None, SimpleNamespace(), transport)

    async def no_report():
        instance.stop.set()
        return None

    async def skip_delay(*_):
        pass

    instance.gateway = no_report
    instance.pause = skip_delay
    await asyncio.wait_for(instance.run(), 2)
    assert attempts == [0, 3] and instance.status["connection"] == "online"


def test_launcher_restores_windows_config_without_echoing_token(tmp_path, monkeypatch, capsys):
    import vfence_windows as launcher

    existing = tmp_path / ".env"
    existing.write_text("SERIAL_PORT=/dev/ttyUSB0\nBASE_TOKEN=private-test-value\n", encoding="utf-8")
    saved = tmp_path / ".env.windows"
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "SETTINGS_FILE", saved)
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [SimpleNamespace(device="COM12", description="ESP")])
    monkeypatch.setattr(launcher, "probe_gateway", lambda port, baud: launcher.GatewayProbe(
        SimpleNamespace(is_open=True), bytearray(), {2: "ONLINE"}, {}, confirmed=True))
    monkeypatch.setattr(launcher, "quartus_directory", lambda _: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(launcher.getpass, "getpass", lambda _prompt: "")
    values, error, _ = launcher.load_settings(False, launcher.GatewayDiscovery())
    assert error is None
    assert values["SERIAL_PORT"] == "COM12" and values["BASE_TOKEN"] == "private-test-value"
    assert launcher.load_settings(True, launcher.GatewayDiscovery())[:2] == (values, None)
    assert "private-test-value" not in capsys.readouterr().out
    assert existing.read_text(encoding="utf-8").startswith("SERIAL_PORT=/dev/ttyUSB0")


async def test_reconcile_discards_old_result_and_confirms_ack():
    instance = service([("PING", "PONG"), ("RESULT", "ERR|NO_RESULT"), ("CLOSE", "VFENCE|P1|4|0"),
                        ("ACK", "OK|ACK")])
    await instance.reconcile()
    assert instance.snapshot()["result"] is None
    assert instance.snapshot()["last_rx"]["line"] == "OK|ACK"
    assert not instance.link.steps


async def test_ack_timeout_leaves_received_result_unconfirmed():
    instance = service([("ACK", TimeoutError("no response"))])
    with pytest.raises(TimeoutError):
        await instance.finish("VFENCE|P0|2|0", 1, "window")
    status = instance.snapshot()
    assert status["last_tx"]["line"] == "ACK"
    assert status["result"]["ack"] is False


async def test_windows_fpga_result_is_committed_before_ack_and_recovered_once(tmp_path):
    from app.database import Database

    db = Database(f"sqlite:///{tmp_path / 'fpga.db'}")
    db.initialize()
    instance = service([])
    instance.config.windows_base = True
    instance.database = db
    wire = "VFENCE|P0|2|0"

    def records():
        with Database(f"sqlite:///{tmp_path / 'fpga.db'}").connect() as con:
            return con.execute("SELECT * FROM windows_fpga_results").fetchall()

    async def failed_ack(command, *_):
        assert command == "ACK" and len(records()) == 1 and records()[0]["acknowledged"] == 0
        raise TimeoutError

    instance.command = failed_ack
    with pytest.raises(TimeoutError):
        await instance.finish(wire, 1, "window")
    instance = FpgaService(instance.config, db, instance.reader)
    instance.link = ScriptedJtag([("PING", "PONG"), ("RESULT", wire), ("ACK", "OK|ACK")])
    await instance.reconcile()
    assert len(records()) == 1 and records()[0]["acknowledged"] == 1
    assert instance.status["result"] is None


async def test_fpga_retries_initial_open_failure_and_requires_pong(caplog):
    config = SimpleNamespace(windows_base=True, fpga_enabled=True, known_collar_ids=("COL01",),
                             hdop_clock_map="{}", gateway_clock_map="{}", metrics_gateway_id="gw")
    link = ScriptedJtag([("PING", "PONG"), ("RESULT", "ERR|NO_RESULT"),
                         ("CLOSE", "ERR|NO_WINDOW"), ("PING", "PONG")])
    link.close = lambda: None
    attempts = []

    def transport(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("JTAG Atlantic open: -5")
        return link

    instance = FpgaService(config, None, SimpleNamespace(), transport)

    async def no_report():
        instance.stop.set()
        return None

    async def no_delay(seconds=3):
        assert seconds == 3

    instance.gateway = no_report
    instance.pause = no_delay
    with caplog.at_level("INFO"):
        await instance.run()
    assert len(attempts) == 2 and not link.steps
    assert instance.status["connection"] == "online"
    assert caplog.text.index("open: -5") < caplog.text.index("PONG CONFIRMADO")


@pytest.mark.parametrize("mode", ["empty", "critical", "normal", "late", "legacy_estimate"])
async def test_evaluation_sends_persisted_pdr_and_finishes(mode, monkeypatch):
    import app.fpga_service as worker

    clock = time.monotonic()
    initial_clock = clock
    utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    report = {"gatewaySessionId": "gateway", "activeProfileId": 1, "operationId": 0, "state": "IDLE"}
    steps = [("BEGIN 1", "OK|BEGIN")]
    expected_result = {"critical": "VFENCE|P1|3|1", "empty": "VFENCE|P1|4|0",
                       "normal": "VFENCE|P0|2|0", "late": "VFENCE|P1|4|0",
                       "legacy_estimate": "VFENCE|P1|4|0"}[mode]
    if mode == "critical":
        steps.extend([("SAMPLE 1 0 4000 0 0 0 0 4", "OK|SAMPLE"),
                      ("SAMPLE 1 1 4000 0 0 0 0 4", expected_result)])
    else:
        if mode == "normal":
            steps.extend((f"SAMPLE 1 {minute} 10000 -500 105 100 5 15", "OK|SAMPLE")
                         for minute in range(3))
        steps.extend([("PING", "PONG"), ("CLOSE", expected_result)])
    steps.append(("ACK", "OK|ACK"))
    instance = service(steps)
    calls = 0

    def records(_ids):
        nonlocal calls, clock
        calls += 1
        rows = []
        if calls > 1 and mode != "empty":
            clock = max(clock, initial_clock + 181)
            rows = [dict(
                gateway_id="gw", collar_id=1, gateway_session_id="gateway", profile_id=1,
                collar_session_id="collar", window_id=f"{minute + 1:016x}",
                started_at_ms=(minute * 60000 - (60000 if mode == "late" else 0)) & 0xFFFFFFFF,
                duration_ms=60000, pdr_x100=4000 if mode == "critical" else 10000, valid=True, received_count=2,
                estimated_tx_count=5 if mode == "critical" else 2,
                estimated_lost_count=3 if mode == "critical" else 0, reason="ok",
                persisted_at=(utc + timedelta(seconds=(minute + 1) * 60 + 1)).isoformat(),
            ) for minute in range({"late": 1, "critical": 2, "normal": 3, "legacy_estimate": 3}[mode])]
        fields = dict(collar_id=1, duration_ms=60000, hdop_mean_x100=None, rssi_mean_x100=None,
                      snr_mean_x100=None, hdop_mean_valid=None, rssi_mean_valid=None, snr_mean_valid=None,
                      gateway_session_id=None, gateway_received_at_ms=None)
        radio = [dict(
            fields, kind="RADIO", producer_id="gw", session_id="gateway", window_id=row["window_id"],
            started_at_ms=row["started_at_ms"], sample_count=5, rssi_mean_x100=-10500, snr_mean_x100=-500,
            rssi_mean_valid=True, snr_mean_valid=True,
            persisted_at=row["persisted_at"],
        ) for row in rows] if mode == "normal" else []
        hdop = [dict(
            fields, kind="HDOP", producer_id="site", session_id="collar", window_id=row["window_id"],
            started_at_ms=row["started_at_ms"], sample_count=10, hdop_mean_x100=100, hdop_mean_valid=True,
            gateway_session_id="gateway", gateway_received_at_ms=row["started_at_ms"] + 60010,
            persisted_at=row["persisted_at"],
        ) for row in rows] if mode == "normal" else []
        return {1: {"pdr": rows, "radio": radio, "hdop": hdop}}

    async def gateway():
        return report

    async def pause(_seconds):
        nonlocal clock
        clock += 301

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(worker, "_utc_now", lambda: (utc + timedelta(seconds=clock - initial_clock)).isoformat())
    instance.gateway = gateway
    instance.records = records
    instance.pause = pause
    clocks = {"gateway": GatewayClock(0, utc, utc + timedelta(minutes=15), timedelta(0))}
    await instance.evaluate([1], {"gateway/collar": 0}, report, clocks, require_proven=mode == "legacy_estimate")
    assert instance.snapshot()["result"]["ack"] is True
    assert instance.snapshot()["result"]["critical"] is (mode == "critical")
    assert not instance.link.steps


async def test_release_closes_com_before_reporting_and_reconnects(monkeypatch):
    import serial

    connections = []

    class Com:
        in_waiting = 0
        is_open = True

        def __init__(self, *_args, **_kwargs):
            assert "write_timeout" not in _kwargs
            connections.append(self)

        def read(self, _):
            time.sleep(0.02)
            return b""

        def close(self):
            self.is_open = False

    async def broadcast(_):
        pass

    async def wait_connection(count):
        while len(connections) < count or reader.state.status != "online":
            await asyncio.sleep(0.005)

    monkeypatch.setattr(serial, "Serial", Com)
    config = SimpleNamespace(mock_serial=False, serial_port="COM5", serial_baud=115200,
                             reconnect_seconds=3, central_sync_enabled=False)
    reader = SerialReader(config, None, SimpleNamespace(broadcast=broadcast))
    task = asyncio.create_task(reader._run_serial())
    try:
        await asyncio.wait_for(wait_connection(1), 2)
        await reader.release_serial()
        assert not connections[0].is_open and reader.state.status == "offline"
        with pytest.raises(ConnectionError):
            await reader.send_command("GET_STATUS")
        reader.resume_serial()
        await asyncio.wait_for(wait_connection(2), 2)
        assert reader.serial_generation == 2
    finally:
        await reader.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("field,value", [
    ("activeProfileId", 3), ("gatewaySessionId", "reboot"), ("operationId", 1),
    ("state", "ROLLING_BACK"),
])
async def test_rechecks_gateway_before_publishing_result(field, value):
    instance = service([("ACK", "OK|ACK")])
    original = {"gatewaySessionId": "gateway", "activeProfileId": 1, "operationId": 0, "state": "IDLE"}

    async def gateway():
        return {**original, field: value}

    instance.gateway = gateway
    utc = datetime.now(timezone.utc)
    await instance.accept_result("VFENCE|P1|4|0", "window", original, 1, None, utc, utc, {})
    assert instance.snapshot()["window"] == "discarded"
    assert instance.snapshot()["result"] is None
    assert not instance.link.steps


@pytest.mark.parametrize("change", ["expired_clock", "collar_reboot", "null_session", "unexpected_without_samples"])
async def test_discards_result_when_its_inputs_are_no_longer_usable(change, monkeypatch):
    import app.fpga_service as worker

    instance = service([("ACK", "OK|ACK")])
    utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observed = utc + timedelta(seconds=90)
    expires = utc + timedelta(seconds=95 if change == "expired_clock" else 300)
    report = {"gatewaySessionId": "gateway", "activeProfileId": 1, "operationId": 0, "state": "IDLE"}
    rows = [{"gateway_id": "gw", "gateway_session_id": "gateway",
             "collar_session_id": None if change == "null_session" else "rebooted",
             "reason": "session_changed" if change == "null_session" else "ok",
             "started_at_ms": 0, "duration_ms": 60000,
             "persisted_at": (utc + timedelta(seconds=95 if change == "null_session" else 61)).isoformat()}
             ] if change in ("collar_reboot", "null_session") else []

    async def gateway():
        return report

    monkeypatch.setattr(worker, "_utc_now", lambda: (utc + timedelta(seconds=100)).isoformat())
    instance.gateway = gateway
    instance.records = lambda _: {1: {"pdr": rows}}
    sessions = {} if change == "unexpected_without_samples" else {1: "collar"}
    await instance.accept_result("VFENCE|P0|2|0", "window", report, 1,
                                 GatewayClock(0, utc, expires, timedelta(0)), utc, observed, sessions)
    assert instance.snapshot()["window"] == "discarded"
    assert instance.snapshot()["result"] is None
    assert not instance.link.steps


def test_gateway_clock_checks_wrap_and_uncertainty():
    utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = GatewayClock(0xFFFFFF00, utc, utc + timedelta(minutes=10), timedelta(milliseconds=5))
    row = {"started_at_ms": 744, "duration_ms": 60000,
           "persisted_at": (utc + timedelta(seconds=62)).isoformat()}
    end = utc + timedelta(seconds=63)
    assert clock.contains(row, utc, end)
    assert not clock.contains(row, utc + timedelta(seconds=1), end)
    assert not clock.contains(row, utc, utc + timedelta(seconds=61))
    assert not clock.contains({**row, "started_at_ms": 0xFFFFFEFF}, utc, end)


def test_serial_clock_checks_round_trip_wrap_drift_and_five_window_ids():
    fields = dict(ok=True, command="GET_METRIC_CLOCK_V1", clockVersion=1, gatewaySessionId="gateway",
                  durationMs=60000, windowStartMs=0xFFFF8000, tickMs=0xFFFF9000, windowId="000000000000000a")
    network = NetworkWindow.from_reply(fields, "gateway", 10, 10.100)
    assert network.first_id == 11 and network.first_tick == 27232
    for offset in range(5):
        assert network.includes(dict(window_id=f"{11 + offset:016x}", started_at_ms=27232 + offset * 60000,
                                     duration_ms=60000))
    assert not network.includes(dict(window_id="0000000000000010", started_at_ms=327232, duration_ms=60000))
    assert NetworkWindow.from_reply(fields, "another", 10, 10.1) is None
    assert NetworkWindow.from_reply(fields, "gateway", 10, 10.3) is None
    next_anchor = NetworkWindow("gateway", 12, 87232, network.projected_start + 60, 0.05)
    assert network.compatible(next_anchor)
    assert not network.compatible(NetworkWindow("gateway", 12, 87232, network.projected_start + 61, 0.05))


async def test_network_window_sends_pdr_only_once_after_pairing_grace(monkeypatch):
    import app.fpga_service as worker

    started = time.monotonic()
    clock = started
    steps = [("BEGIN 1", "OK|BEGIN"), ("PING", "PONG"), ("SAMPLE 1 0 4000 0 0 0 0 4", "OK|SAMPLE"),
             ("PING", "PONG"), ("PING", "PONG"), ("CLOSE", "VFENCE|P1|4|0"), ("ACK", "OK|ACK")]
    instance = service(steps)
    report = {"gatewaySessionId": "gateway", "activeProfileId": 1, "operationId": 0, "state": "IDLE"}
    network = NetworkWindow("gateway", 1, 0, started, 0.01)
    row = dict(gateway_id="gw", collar_id=1, gateway_session_id="gateway", profile_id=1,
               collar_session_id="collar", window_id="0000000000000001", started_at_ms=0, duration_ms=60000,
               pdr_x100=4000, valid=True, received_count=2, estimated_tx_count=5, estimated_lost_count=3,
               reason="ok", tx_proven=1)

    async def clock_probe(_session):
        return network

    async def gateway():
        return report

    calls = 0

    def records(_ids):
        nonlocal clock, calls
        calls += 1
        if calls == 2:
            clock = started + 62
        return {1: {"pdr": [row] if calls > 1 else [], "radio": [], "hdop": []}}

    async def pause(_seconds):
        nonlocal clock
        clock += 16 if clock < started + 90 else 300

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock))
    instance.network_window = clock_probe
    instance.gateway = gateway
    instance.records = records
    instance.pause = pause
    await instance.evaluate([1], {}, report, network=network, require_proven=True)
    assert not instance.link.steps
    assert instance.status["result"]["ack"]


def test_native_late_reply_is_not_returned():
    driver = JtagAtlantic.__new__(JtagAtlantic)
    driver.handle = object()
    driver.pending = bytearray()
    driver.write = lambda _handle, _data, length: length
    driver.flush = lambda _handle: 0

    def late_reply():
        time.sleep(0.04)
        return b"PONG\n"

    driver._read = late_reply
    with pytest.raises(TimeoutError):
        driver.command("PING", timeout=0.02)


def _stalled_jtag_worker(pipe, _settings):
    pipe.send(("ok", None, False))
    pipe.recv()
    time.sleep(30)


@pytest.mark.parametrize("stall", ["command", "close"])
def test_stalled_native_process_is_reaped(stall):
    transport = IsolatedJtagAtlantic(None, worker=_stalled_jtag_worker)
    started = time.monotonic()
    try:
        if stall == "command":
            with pytest.raises(TimeoutError):
                transport.command("PING", timeout=0.1)
    finally:
        transport.close()
    assert time.monotonic() - started < 4
    with pytest.raises(ValueError, match="closed"):
        transport.process.is_alive()


async def test_windows_probe_and_release_with_write_pending(monkeypatch):
    import serial

    connections = []

    class Com:
        is_open = True

        def __init__(self, *_args, **kwargs):
            assert kwargs["write_timeout"] == 2
            self.replies = deque()
            self.allow_reply = threading.Event()
            self.write_started = threading.Event()
            self.write_cancelled = threading.Event()
            self.probe_sent = threading.Event()
            connections.append(self)

        @property
        def in_waiting(self):
            return len(self.replies[0]) if self.replies and self.allow_reply.is_set() else 0

        def read(self, _):
            if self.in_waiting:
                return self.replies.popleft()
            time.sleep(0.005)
            return b""

        def write(self, line):
            if b"GET_STATUS" in line:
                request = json.loads(line.split(b"[CMD] ", 1)[1])
                response = {**request, "kind": "command_response", "ok": True}
                self.replies.append(b"[APP] " + json.dumps(response).encode() + b"\n")
                self.probe_sent.set()
                return len(line)
            self.write_started.set()
            assert self.write_cancelled.wait(2)
            return 0

        def cancel_write(self):
            self.write_cancelled.set()

        def flush(self):
            pytest.fail("Windows must not wait in flush()")

        def close(self):
            assert not self.write_started.is_set() or self.write_cancelled.is_set()
            self.is_open = False

    async def broadcast(_):
        pass

    async def wait_for(check):
        async def spin():
            while not check():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(spin(), 2)

    monkeypatch.setattr(serial, "Serial", Com)
    config = SimpleNamespace(windows_base=True, mock_serial=False, serial_port="COM5", serial_baud=115200,
                             reconnect_seconds=3, central_sync_enabled=False, max_line_length=2048)
    reader = SerialReader(config, None, SimpleNamespace(broadcast=broadcast))
    task = asyncio.create_task(reader._run_serial())
    try:
        await wait_for(lambda: connections and connections[0].probe_sent.is_set())
        assert reader.as_dict()["serial_open"] and reader.state.status == "offline"
        with pytest.raises(ConnectionError):
            await reader.send_command("SET_PARAMETERS")
        connections[0].allow_reply.set()
        await wait_for(lambda: reader.state.status == "online")
        write = asyncio.create_task(reader.send_command("SET_PARAMETERS"))
        await wait_for(connections[0].write_started.is_set)
        await reader.release_serial()
        with pytest.raises(OSError, match="incomplete"):
            await write
        assert not connections[0].is_open and not reader.as_dict()["serial_open"]
        reader.resume_serial()
        await wait_for(lambda: len(connections) == 2 and connections[1].probe_sent.is_set())
        assert reader.state.status == "offline"
        connections[1].allow_reply.set()
        await wait_for(lambda: reader.state.status == "online")
    finally:
        await reader.stop()
        await asyncio.wait_for(task, 3)
