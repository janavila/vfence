import asyncio
import os
import socket
import subprocess
import sys
import time
import json
import warnings
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import vfence_windows as launcher
from app.database import Database
from app.serial_reader import SerialReader
from test_metric_summaries import FakeByteConnection, RecordingHub, hdop_json, radio_json, serial_line


@pytest.mark.skipif(os.name != "nt", reason="runs a real Windows launcher process")
def test_cold_process_without_input_recovers_gateway_and_keeps_binding(tmp_path):
    from dotenv import dotenv_values

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    path = tmp_path / ".env.windows"
    path.write_text(f"VFENCE_PORT={port}\nCOLLAR_IDS=COL01\nVFENCE_FPGA_ENABLED=false\n"
                    f"DATABASE_URL=sqlite:///{(tmp_path / 'cold.db').as_posix()}\n"
                    "CENTRAL_SYNC_ENABLED=false\nSERIAL_RECONNECT_SECONDS=0.05\n", encoding="utf-8")
    program = r"""
import json, os, sys
from pathlib import Path
import vfence_windows as launcher
sys.path.insert(0, str(launcher.ROOT / "tests"))
from test_metric_summaries import FakeByteConnection, serial_line

launcher.ROOT = Path(os.environ["VFENCE_TEST_ROOT"])
launcher.SETTINGS_FILE = launcher.ROOT / ".env.windows"
launcher.BASE_MUTEX_NAME = "Local\\VFence.Test." + str(os.getpid())
launcher.gateway_usb_ids = lambda: {"COM12": "test-device"} if (launcher.ROOT / "plugged").exists() else {}
schema = launcher.SERIAL_SCHEMA

class Wire(FakeByteConnection):
    def write(self, data):
        command = json.loads(data.split(b"[CMD] ", 1)[1])
        self.feed(serial_line({**command, "schema": schema, "kind": "command_response", "ok": True}))
        return super().write(data)

def probe(port, baud):
    assert baud == 115200 and port == "COM12"
    wire = Wire()
    wire.port = port
    frame = serial_line(dict(schema=schema, kind="telemetry", collarId=1, sequence=31,
                             gpsValid=True, inside=True, latitude=-31.3, longitude=-54.0))
    return launcher.GatewayProbe(wire, bytearray(frame), {1: "ONLINE"},
        {"gatewaySessionId": "1111111111111111"}, confirmed=True)

launcher.probe_gateway = probe
launcher.main()
"""
    env = {**os.environ, "VFENCE_TEST_ROOT": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    url = f"http://127.0.0.1:{port}"

    def start():
        return subprocess.Popen([sys.executable, "-c", program], cwd=launcher.ROOT, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_for(process, route, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                response = httpx.get(url + route, timeout=0.5, trust_env=False)
                if response.is_success and predicate(response):
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        pytest.fail(f"{route} did not become ready (exit {process.poll()})")

    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)

    first = start()
    try:
        wait_for(first, "/", lambda response: "VFence" in response.text)
        wait_for(first, "/api/gateway", lambda response: response.json()["status"] == "offline")
        (tmp_path / "plugged").touch()
        wait_for(first, "/api/collars", lambda response: any(row["collar_id"] == "COL01" for row in response.json()))
        wait_for(first, "/api/gateway", lambda response: response.json()["status"] == "online"
                 and response.json()["serial_port"] == "COM12" and response.json()["commit_config"]["metrics"])
        saved = dotenv_values(path, interpolate=False)
    finally:
        stop(first)
    second = start()
    try:
        wait_for(second, "/api/gateway", lambda response: response.json()["status"] == "online")
        restarted = dotenv_values(path, interpolate=False)
        for field in ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID"):
            assert saved[field] and restarted[field] == saved[field]
    finally:
        stop(second)


@pytest.mark.skipif(os.name != "nt", reason="credential storage uses Windows DPAPI")
def test_verified_central_token_is_protected_and_reused(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    path = tmp_path / ".env.windows"
    path.write_text("CENTRAL_SYNC_ENABLED=true\nVFENCE_FPGA_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "SETTINGS_FILE", path)
    monkeypatch.setattr(launcher, "settings_menu", lambda *_: (True, None))
    token = "local-test-token-never-logged"
    monkeypatch.setattr(launcher, "read_token", lambda _: token)
    monkeypatch.setattr(launcher, "check_central",
                        lambda values: "autenticada" if values["BASE_TOKEN"] == token else "token ausente")
    values, error, _ = launcher.load_settings(False, launcher.GatewayDiscovery())
    assert error is None and values["BASE_TOKEN"] == token
    stored = dotenv_values(path, interpolate=False)
    assert stored[launcher.PROTECTED_TOKEN] and "BASE_TOKEN" not in stored
    assert token not in path.read_text(encoding="utf-8")
    monkeypatch.setattr(launcher, "read_token", lambda *_: pytest.fail("asked for token again"))
    monkeypatch.setattr(launcher, "check_central", lambda *_: pytest.fail("blocked startup on preflight"))
    monkeypatch.setattr(launcher, "select_gateway", lambda *_: None)
    assert launcher.load_settings(True, launcher.GatewayDiscovery())[0]["BASE_TOKEN"] == token


@pytest.mark.parametrize(("flags", "automatic"), [([], True), (["--reuse"], True), (["--configure"], False)])
def test_windows_cli_defaults_to_unattended(flags, automatic, monkeypatch):
    monkeypatch.setattr(launcher.sys, "argv", ["vfence_windows.py", *flags])
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(launcher.os, "chdir", lambda _: None)
    monkeypatch.setattr(launcher.os, "environ", {})
    mutex = Mock()
    mutex.CreateMutexW.return_value = 1
    monkeypatch.setattr(launcher.ctypes, "WinDLL", lambda *a, **k: mutex, raising=False)
    monkeypatch.setattr(launcher.ctypes, "get_last_error", lambda: 0, raising=False)
    load = Mock(return_value=({}, None, None))
    monkeypatch.setattr(launcher, "load_settings", load)
    launched = []

    async def launch(*args):
        launched.append(args)

    monkeypatch.setattr(launcher, "launch", launch)
    launcher.main()
    assert load.call_args.args[0] is automatic
    assert launched == [(None, None)]


def test_unattended_metrics_identity_survives_com_move_and_offline(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    path = tmp_path / ".env.windows"
    path.write_text("SERIAL_PORT=COM5\nCOLLAR_IDS=COL01\nVFENCE_FPGA_ENABLED=false\n"
                    "VFENCE_GATEWAY_USB_ID=bound-usb\nCENTRAL_SYNC_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "SETTINGS_FILE", path)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("unattended input"))
    monkeypatch.setattr(launcher, "check_central", lambda *_: pytest.fail("HTTP before startup"))
    devices = {}
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: devices)
    monkeypatch.setattr(launcher, "probe_gateway", lambda port, baud: launcher.GatewayProbe(
        SimpleNamespace(is_open=True, port=port), bytearray(b"data"), {1: "ONLINE"},
        {"gatewaySessionId": "1000000000000000"}, confirmed=True))
    offline, error, probe = launcher.load_settings(True, launcher.GatewayDiscovery())
    assert error is None and probe is None and offline["CENTRAL_SYNC_ENABLED"] == "true"
    keys = ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID", "METRICS_SERIAL_ACK_ENABLED")
    persisted = dotenv_values(path, interpolate=False)
    assert persisted[keys[0]].startswith("local-gw-") and persisted[keys[1]].startswith("local-site-")
    assert persisted[keys[2]] == "true"
    devices.update(COM5="unrelated", COM8="bound-usb")
    resumed, _, probe = launcher.load_settings(True, launcher.GatewayDiscovery())
    assert resumed["SERIAL_PORT"] == "COM8" and probe.detected == {1: "ONLINE"}
    assert all(resumed[key] == persisted[key] for key in keys)
    devices.clear()
    devices.update(COM8="unrelated", COM12="bound-usb")
    connection, backlog = launcher.checked_reopen("COM8", 115200, probe)
    assert connection.port == "COM12" and backlog == b"data"


def test_detached_capture_without_status_requires_previously_bound_usb(monkeypatch):
    backlog = bytearray(serial_line(dict(schema=launcher.SERIAL_SCHEMA, kind="telemetry", collarId=1, sequence=5)))
    devices = {"COM5": "bound"}
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: devices)
    monkeypatch.setattr(launcher, "probe_gateway", lambda port, baud: launcher.GatewayProbe(
        SimpleNamespace(is_open=False, port=port), backlog, {}, {}, disconnected=True))
    saved = dict(launcher.DEFAULTS, SERIAL_PORT="COM5", VFENCE_GATEWAY_USB_ID="bound")
    probe = launcher.select_gateway(saved, launcher.GatewayDiscovery())
    assert probe.disconnected and not probe.confirmed and probe.backlog == backlog
    devices["COM5"] = "different"
    assert launcher.select_gateway(saved, launcher.GatewayDiscovery()) is None


def test_unattributed_capture_is_durable_and_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "SETTINGS_FILE", tmp_path / ".env.windows")
    first = serial_line(dict(schema=launcher.SERIAL_SCHEMA, kind="telemetry", collarId=1, sequence=5))
    second = serial_line(dict(schema=launcher.SERIAL_SCHEMA, kind="telemetry", collarId=2, sequence=6))
    devices = {"COM5": ""}
    fake = FakeByteConnection()
    fake.port = "COM5"
    fake.close()
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: devices)
    monkeypatch.setattr(launcher, "probe_gateway", lambda *_: launcher.GatewayProbe(
        fake, bytearray(first + second[:-1]), {}, {}, disconnected=True))
    discovery = launcher.GatewayDiscovery()
    assert launcher.select_gateway(launcher.DEFAULTS.copy(), discovery) is None
    discovery.close()
    captured, = (tmp_path / ".vfence-unattributed").glob("*.capture")
    assert captured.read_bytes() == first
    assert fake.writes == []


async def test_live_discovery_survives_restart_without_authorizing_unknown_collar(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'collars.db'}"
    db = Database(database_url)
    db.initialize()
    config = SimpleNamespace(windows_base=True, mock_serial=False, serial_port="COM5",
                             central_sync_enabled=False, known_collar_ids=("COL01",))
    reader = SerialReader(config, db, RecordingHub())
    for collar in (1, 254):
        data = dict(schema=launcher.SERIAL_SCHEMA, kind="collar_status", collarId=collar, state="ONLINE")
        assert await reader.process_line(serial_line(data))
    assert db.windows_detected_collars() == [1, 254]
    assert await reader.process_line(serial_line(dict(schema=launcher.SERIAL_SCHEMA, kind="collar_state",
                                                      collarId=1, state="OFFLINE")))
    assert reader.collar_contacts["COL01"]["state"] == "offline"
    assert config.known_collar_ids == ("COL01",)
    assert Database(database_url).windows_detected_collars() == [1, 254]
    assert SerialReader(config, db, RecordingHub()).collar_contacts == {}


def test_probe_requires_matching_wire_response_and_keeps_port_for_handoff(monkeypatch):
    connections = []
    clock = 0

    class Serial:
        in_waiting = 512

        def __init__(self, **kwargs):
            assert kwargs["baudrate"] == 115200
            connections.append(self)
            self.replies = bytearray()
            self.commands = []

        def close(self):
            self.is_open = False

        def open(self):
            assert self.port == "COM5" and not self.rts and not self.dtr
            self.is_open = True

        def write(self, packet):
            request = json.loads(packet.split(b"[CMD] ", 1)[1])
            self.commands.append(request["command"])
            reply = {**request, "kind": "command_response", "ok": True}
            if request["command"] == "GET_STATUS":
                data = [
                    {**reply, "requestId": request["requestId"] + 2},
                    {**reply, "schema": "other"},
                ]
                if len(self.commands) > 1:
                    data.extend([
                        reply, {"schema": launcher.SERIAL_SCHEMA, "kind": "collar_status",
                                "collarId": 2, "state": "ONLINE"},
                    ])
            else:
                data = [{**reply, "profileControlVersion": 2, "participants": [{"collarId": 2}]}]
            for entity in data:
                self.replies.extend(b"[APP] " + json.dumps(entity).encode("ascii") + b"\r\n")
            return len(packet)

        def read(self, _):
            nonlocal clock
            clock += 0.05
            read = self.replies[:67]
            del self.replies[:67]
            return bytes(read)

    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr("serial.Serial", Serial)
    probe = launcher.probe_gateway("COM5", 115200)
    assert probe.confirmed and probe.detected == {2: "ONLINE"}
    assert probe.profile["participants"] == [{"collarId": 2}]
    assert connections[0].commands == ["GET_STATUS", "GET_STATUS", "GET_PROFILE_V2"]
    assert probe.connection is connections[0] and probe.connection.is_open
    probe.close()
    assert not connections[0].is_open


def test_discovery_rejects_ambiguity_and_retains_offline_collar(monkeypatch, capsys):
    values = {**launcher.DEFAULTS, "COLLAR_IDS": "COL01,COL02"}
    monkeypatch.setattr("serial.tools.list_ports.comports",
                        lambda: [SimpleNamespace(device=port) for port in ("COM5", "COM8", "COM9")])
    def probe(port, baud):
        return launcher.GatewayProbe(SimpleNamespace(is_open=True), bytearray(), {1: "OFFLINE", 3: "ONLINE"},
                                     {}, confirmed=port != "COM9")
    monkeypatch.setattr(launcher, "probe_gateway", probe)
    discovery = launcher.GatewayDiscovery()
    with pytest.raises(ValueError, match="Mais de um"):
        launcher.select_gateway(values, discovery)
    values["SERIAL_PORT"] = "COM5"
    assert launcher.select_gateway(values, discovery).confirmed
    assert values["COLLAR_IDS"] == "COL01,COL02"
    assert "Offline: COL01" in capsys.readouterr().out
    with pytest.raises(ValueError, match="repetidos"):
        launcher.collar_ids("002,col02")


@pytest.mark.parametrize("outcome", [200, 401, 403, 503])
def test_central_only_prompts_on_invalid_credential(outcome, monkeypatch, capsys):
    values = {**launcher.DEFAULTS, "CENTRAL_SYNC_ENABLED": "true", "BASE_TOKEN": "private-original"}
    calls = []

    def post(url, *, headers, json, timeout):
        assert url.endswith("/api/edge/batch")
        assert json == {"base_id": values["BASE_ID"], "items": []}
        calls.append(headers["Authorization"])
        status = outcome if len(calls) == 1 else 200
        return httpx.Response(status, json={"acked_up_to": 0})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(launcher.getpass, "getpass", lambda _: "private-replacement")
    assert launcher.prepare_central(values, interactive=True) == {
        200: "autenticada", 401: "autenticada", 403: "HTTP 403", 503: "indisponível",
    }[outcome]
    assert len(calls) == (2 if outcome == 401 else 1)
    assert values["BASE_TOKEN"] == ("private-replacement" if outcome == 401 else "private-original")
    assert "private-" not in capsys.readouterr().out


def test_hidden_input_never_falls_back_to_echo(monkeypatch, capsys):
    def fallback(_):
        warnings.warn("stdin echo", launcher.getpass.GetPassWarning)
        pytest.fail("input must stop before an echoed prompt")

    monkeypatch.setattr(launcher.getpass, "getpass", fallback)
    assert launcher.read_token("Token: ") == ""
    assert "entrada oculta" in capsys.readouterr().out


def test_fpga_and_ack_preflight_degrade_without_changing_saved_values(monkeypatch, capsys):
    values = {**launcher.DEFAULTS, "VFENCE_GATEWAY_CLOCK_MAP": "[]", "COLLAR_IDS": "COL02",
              "METRICS_SERIAL_ACK_ENABLED": "true", "PDR_SERIAL_COMMIT_ENABLED": "true"}
    runtime = values.copy()
    monkeypatch.setattr(launcher, "quartus_directory", lambda _: pytest.fail("invalid maps must stop before discovery"))
    launcher.prepare_acks(values, runtime, gateway_verified=True)
    launcher.validate_fpga(values, runtime)
    assert runtime["VFENCE_FPGA_ENABLED"] == runtime["METRICS_SERIAL_ACK_ENABLED"] == "false"
    assert runtime["PDR_SERIAL_COMMIT_ENABLED"] == "false"
    assert values["VFENCE_FPGA_ENABLED"] == values["METRICS_SERIAL_ACK_ENABLED"] == "true"
    assert "confira VFENCE_GATEWAY_CLOCK_MAP" in capsys.readouterr().out


def test_missing_quartus_during_preflight_keeps_fpga_retry_enabled(monkeypatch):
    values = {**launcher.DEFAULTS, "COLLAR_IDS": "COL01"}
    runtime = values.copy()
    monkeypatch.setattr(launcher, "quartus_directory", lambda _: None)
    assert launcher.validate_fpga(values, runtime) is False
    assert runtime["VFENCE_FPGA_ENABLED"] == "true"


def test_empty_clock_maps_allow_diagnostics_and_keep_dev_instance(monkeypatch, tmp_path, capsys):
    values = {**launcher.DEFAULTS, "COLLAR_IDS": "COL02"}
    runtime = values.copy()
    monkeypatch.setattr(launcher, "quartus_directory", lambda _: tmp_path)
    monkeypatch.setattr(launcher, "usb_blaster_present", lambda: True)
    launcher.validate_fpga(values, runtime)
    assert runtime["VFENCE_FPGA_ENABLED"] == "true"
    assert runtime["VFENCE_JTAG_DEVICE"] == "1" and runtime["VFENCE_JTAG_INSTANCE"] == "0"
    assert "FPGA em diagnóstico" in capsys.readouterr().out


@pytest.mark.parametrize("rejection", ["HTTP 401", "HTTP 403", "indisponível", "resposta ou endereço da Central incompatível"])
def test_invalid_service_edits_do_not_replace_saved_values(rejection, monkeypatch, tmp_path):
    from dotenv import dotenv_values

    path = tmp_path / ".env.windows"
    path.write_text(
        "SERIAL_PORT=COM5\nCOLLAR_IDS=COL02\nCENTRAL_SYNC_ENABLED=true\nCENTRAL_BASE_URL=https://saved.example\n"
        "BASE_ID=prior\nBASE_TOKEN=old-test-token\nVFENCE_GATEWAY_CLOCK_MAP={}\n", encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "SETTINGS_FILE", path)
    monkeypatch.setattr(launcher, "read_token", lambda _: "")
    monkeypatch.setattr(launcher, "quartus_directory", lambda _: tmp_path)
    monkeypatch.setattr(launcher, "usb_blaster_present", lambda: True)
    monkeypatch.setattr(launcher, "check_central",
                        lambda values: "autenticada" if values["BASE_ID"] == "prior" else rejection)

    def menu(values, _):
        values.update(BASE_ID="new", CENTRAL_BASE_URL="https://edited.example", VFENCE_GATEWAY_CLOCK_MAP="[]",
                      SERIAL_PORT="COM7", SERIAL_BAUD="9600", COLLAR_IDS="COL02,COL03")
        return True, None

    monkeypatch.setattr(launcher, "settings_menu", menu)
    runtime, error, _ = launcher.load_settings(False, launcher.GatewayDiscovery())
    saved = dotenv_values(path)
    assert error is None
    for values in (saved, runtime):
        assert values["CENTRAL_BASE_URL"] == "https://saved.example"
        assert values["BASE_ID"] == "prior"
        assert values["BASE_TOKEN"] == "old-test-token"
        assert values["VFENCE_GATEWAY_CLOCK_MAP"] == "{}"
        assert values["SERIAL_PORT"] == "COM5" and values["SERIAL_BAUD"] == "115200"
        assert values["COLLAR_IDS"] == "COL02,COL03"


@pytest.mark.parametrize("port,usb_id,boot,interactive,answers,expected", [
    ("COM7", "usb-A", "2222222222222222", False, [], "original"),
    ("COM7", "", "1111111111111111", False, [], "original"),
    ("COM5", "usb-B", "2222222222222222", False, [], ""),
    ("COM7", "", "2222222222222222", False, [], ""),
    ("COM7", "", "2222222222222222", True, ["s"], "original"),
    ("COM5", "usb-B", "2222222222222222", True, ["original"], ""),
    ("COM5", "usb-B", "2222222222222222", True, ["provisioned-new"], "provisioned-new"),
])
def test_bind_metrics_only_to_proven_or_confirmed_gateway(port, usb_id, boot, interactive, answers, expected,
                                                            monkeypatch):
    previous = {**launcher.DEFAULTS, "SERIAL_PORT": "COM5", "METRICS_GATEWAY_ID": "original",
                "METRICS_INSTALLATION_ID": "installation", "METRICS_SERIAL_ACK_ENABLED": "true",
                "VFENCE_GATEWAY_USB_ID": "usb-A", "VFENCE_GATEWAY_BOOT": "1111111111111111"}
    values = {**previous, "SERIAL_PORT": port}
    runtime = values.copy()
    probe = launcher.GatewayProbe(SimpleNamespace(is_open=True), bytearray(), {}, {"gatewaySessionId": boot},
                                  usb_id=usb_id, confirmed=True)
    answers = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    launcher.prepare_gateway(values, runtime, previous, probe, interactive=interactive)
    assert runtime["METRICS_GATEWAY_ID"] == expected
    assert runtime["SERIAL_PORT"] == port
    if not expected:
        assert runtime["METRICS_SERIAL_ACK_ENABLED"] == "false"
        assert runtime["METRICS_INSTALLATION_ID"] == ""
        assert values == previous
    else:
        assert values["SERIAL_PORT"] == port and values["METRICS_GATEWAY_ID"] == expected


def test_reconnect_refuses_different_hardware_at_the_same_port(monkeypatch):
    initial = launcher.GatewayProbe(SimpleNamespace(is_open=False), bytearray(), {}, {},
                                    usb_id="adapter-A", confirmed=True)
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: {"COM5": "adapter-B"})
    monkeypatch.setattr(launcher, "probe_gateway", lambda *args: pytest.fail("must not open the replacement"))
    with pytest.raises(ConnectionError, match="Outro Gateway"):
        launcher.checked_reopen("COM5", 115200, initial)


def test_reconnect_without_usb_serial_keeps_bytes_only_for_the_same_boot(monkeypatch):
    profile = {"gatewaySessionId": "1234567890abcdef"}
    connection = FakeByteConnection()
    initial = launcher.GatewayProbe(SimpleNamespace(is_open=False), bytearray(), {}, profile, confirmed=True)
    current = launcher.GatewayProbe(connection, bytearray(b"partial"), {}, dict(profile), confirmed=True)
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: {"COM5": ""})
    monkeypatch.setattr(launcher, "probe_gateway", lambda *args: current)
    assert launcher.checked_reopen("COM5", 115200, initial) == (connection, b"partial")
    current.profile["gatewaySessionId"] = "1111111111111111"
    with pytest.raises(ConnectionError, match="não verificada"):
        launcher.checked_reopen("COM5", 115200, initial)
    assert connection.closed


async def test_probe_handoff_persists_backlog_and_completes_fragment_before_acks(monkeypatch):
    db = Database("sqlite:///:memory:")
    db.initialize()
    config = SimpleNamespace(windows_base=True, mock_serial=False, serial_port="COM5", serial_baud=115200,
                             reconnect_seconds=3, central_sync_enabled=False, max_line_length=512,
                             metrics_installation_id="installation", metrics_gateway_id="gateway",
                             metrics_serial_ack_enabled=True, event_serial_commit_enabled=True)
    schema = launcher.SERIAL_SCHEMA
    telemetry = serial_line(dict(schema=schema, kind="telemetry", collarId=2, sequence=7, gpsValid=True,
                                 inside=True, latitude=-31.3, longitude=-54.0))
    event = serial_line(dict(schema=schema, kind="event", collarId=2, subtype="OUTSIDE",
                             deliveryVersion=1, deliveryId="ab" * 12))
    radio = serial_line(radio_json())
    hdop = serial_line(hdop_json())
    cut = len(radio) // 2

    class Port(FakeByteConnection):
        def write(self, data):
            request = json.loads(data.split(b"[CMD] ", 1)[1])
            if request["command"] == "GET_STATUS":
                self.feed(serial_line({**request, "kind": "command_response", "ok": True}))
            if request["command"] == "METRIC_COMMITTED":
                kind = request["kind"].split("_")[0]
                assert db.link_metrics(1, 1)["windows"][kind]
            if request["command"] == "EVENT_COMMITTED":
                assert any(item["event_type"] == "outside" for item in db.events())
            return super().write(data)

    connection = Port(radio[cut:])
    connection.port = "COM5"
    monkeypatch.setattr("serial.Serial", lambda *a, **k: pytest.fail("port must not be reopened"))
    reader = SerialReader(config, db, RecordingHub())
    reader.adopt_windows_serial(connection, telemetry + event + hdop + radio[:cut])
    task = asyncio.create_task(reader.run())
    try:
        async def drained():
            while reader.state.status != "online" or sum(b"METRIC_COMMITTED" in item for item in connection.writes) < 2:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(drained(), 3)
        assert [row["sequence"] for row in db.telemetry("COL02")] == [7]
        assert len([row for row in db.events() if row["event_type"] == "outside"]) == 1
        assert all(len(db.link_metrics(1, 10)["windows"][kind]) == 1 for kind in ("hdop", "radio"))
        assert sum(b"EVENT_COMMITTED" in item for item in connection.writes) == 1
    finally:
        await reader.stop()
        await asyncio.wait_for(task, 5)
    assert connection.closed


@pytest.mark.parametrize("key", ["VFENCE_GATEWAY_CLOCK_MAP", "VFENCE_HDOP_CLOCK_MAP"])
def test_disabled_fpga_cannot_overwrite_valid_map(monkeypatch, tmp_path, key):
    from dotenv import dotenv_values

    path = tmp_path / ".env.windows"
    path.write_text("SERIAL_PORT=COM5\nVFENCE_FPGA_ENABLED=false\n" + key + "={}\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "SETTINGS_FILE", path)

    def menu(values, _discovery):
        values[key] = "[]"
        values["COLLAR_IDS"] = "COL02"
        return True, None

    monkeypatch.setattr(launcher, "settings_menu", menu)
    monkeypatch.setattr(launcher, "quartus_directory", lambda *_: pytest.fail("FPGA is disabled"))
    runtime, error, _ = launcher.load_settings(False, launcher.GatewayDiscovery())
    saved = dotenv_values(path)
    assert error is None
    assert saved[key] == runtime[key] == "{}"
    assert saved["COLLAR_IDS"] == "COL02"
    assert saved["VFENCE_FPGA_ENABLED"] == runtime["VFENCE_FPGA_ENABLED"] == "false"


async def test_disconnected_probe_replays_only_complete_frames_without_ack(monkeypatch):
    schema = launcher.SERIAL_SCHEMA
    position = serial_line(dict(schema=schema, kind="telemetry", collarId=2, sequence=31,
                                gpsValid=True, inside=True, latitude=-31.3, longitude=-54.0))
    event = serial_line(dict(schema=schema, kind="event", collarId=2, subtype="OUTSIDE",
                             deliveryVersion=1, deliveryId="ab" * 12))
    hdop = serial_line(hdop_json())
    radio = serial_line(radio_json())
    captured_at = {}

    class Port(FakeByteConnection):
        def open(self):
            self.is_open = True

        def write(self, wire):
            assert self.is_open
            request = json.loads(wire.split(b"[CMD] ", 1)[1])
            if request["command"] == "GET_STATUS":
                self.feed(serial_line({**request, "kind": "command_response", "ok": True}))
            elif request["command"] == "GET_PROFILE_V2":
                self.feed(serial_line({**request, "kind": "command_response", "ok": True,
                                       "profileControlVersion": 2, "gatewaySessionId": "1111111111111111"}),
                          position, event, hdop, radio[:len(radio)//2], OSError("COM retirada"))
            else:
                pytest.fail("disconnected capture cannot cause an ACK")
            return super().write(wire)

    port = Port()
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    monkeypatch.setattr(launcher, "gateway_usb_ids", lambda: captured_at)
    captured_at["COM5"] = "original-adapter"
    previous = {**launcher.DEFAULTS, "SERIAL_PORT": "COM5", "METRICS_GATEWAY_ID": "gateway",
                "METRICS_INSTALLATION_ID": "installation", "VFENCE_GATEWAY_USB_ID": "original-adapter"}
    values = previous.copy()
    discovery = launcher.GatewayDiscovery()
    probe = launcher.select_gateway(values, discovery)
    assert probe.confirmed and probe.disconnected and port.closed
    # Another device now appears at COM5. The capture still belongs to the old one.
    captured_at["COM5"] = "replacement-adapter"
    assert launcher.select_gateway(values, discovery) is probe
    assert probe.usb_id == "original-adapter"
    runtime = values.copy()
    launcher.prepare_gateway(values, runtime, previous, probe, interactive=False)
    assert runtime["METRICS_GATEWAY_ID"] == "gateway"

    db = Database("sqlite:///:memory:")
    db.initialize()
    config = SimpleNamespace(windows_base=True, mock_serial=False, serial_port="COM5", serial_baud=115200,
                             reconnect_seconds=3, central_sync_enabled=False, max_line_length=512,
                             metrics_installation_id=runtime["METRICS_INSTALLATION_ID"],
                             metrics_gateway_id=runtime["METRICS_GATEWAY_ID"], metrics_serial_ack_enabled=True,
                             event_serial_commit_enabled=True)
    reader = SerialReader(config, db, RecordingHub())
    reader.adopt_windows_serial(port, bytes(probe.backlog), detached=probe.disconnected)
    reader.set_windows_serial_reopen(lambda: launcher.checked_reopen("COM5", 115200, probe))
    task = asyncio.create_task(reader.run())
    try:
        async def drained():
            while not db.link_metrics(1, 1)["windows"]["hdop"]:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(drained(), 3)
        assert [row["sequence"] for row in db.telemetry("COL02")] == [31]
        assert any(row["event_type"] == "outside" for row in db.events())
        assert db.link_metrics(1, 1)["windows"]["hdop"][0]["producer_id"] == "installation"
        assert not db.link_metrics(1, 1)["windows"]["radio"]
        assert not any(b"_COMMITTED" in wire for wire in port.writes)
    finally:
        await reader.stop()
        await asyncio.wait_for(task, 5)
