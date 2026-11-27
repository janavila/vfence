import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from app.database import Database
from app.fpga_service import FpgaService, NetworkWindow
from app.fpga_window import FpgaWindow
from app.jtag_atlantic import sample_command
from app.metric_evidence import parse_evidence
from app.serial_reader import SerialReader
from test_metric_summaries import FakeByteConnection, RecordingHub


@pytest.fixture
def evidence_commit_host(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    use_wsl = os.getenv("VFENCE_TEST_WSL") == "1"
    if not compiler and not use_wsl:
        pytest.skip("requires host C++ compiler or VFENCE_TEST_WSL=1 with Ubuntu and g++")

    def host_path(path):
        if use_wsl:
            return subprocess.check_output(["wsl.exe", "-d", "Ubuntu", "--exec", "wslpath", "-a", str(path)],
                                           text=True).strip()
        return str(path)

    root = Path(__file__).resolve().parents[1]
    gateway = root / "gateway/VFenceGateway"
    sketch = (gateway / "VFenceGateway.ino").read_text(encoding="utf-8")
    schema = next(line for line in sketch.splitlines() if line.startswith("static const char* SERIAL_SCHEMA = "))
    lookup = sketch[sketch.index("static bool metricWindowFor"):sketch.index("static bool handleEvidenceControl")]
    dispatch = sketch[sketch.index("void processJsonCommand"):sketch.index("  if (processProfileTransition(json, command")]
    # Keep the production checks and state updates, using std::string in place of Arduino String.
    dispatch = (dispatch[:dispatch.index('  if (command == "METRIC_COMMITTED")')]
                + dispatch[dispatch.index('  if (!json.stringField("schema"'):dispatch.index('  if (command == "ENABLE_EVENT_COMMIT")')]
                + dispatch[dispatch.index('  if (command == "EVIDENCE_COMMITTED")'):]
                + "}\n")
    dispatch = dispatch.replace("const String", "const std::string").replace(
        "line.substring(commandOffset, commandOffset + length)", "line.substr(commandOffset, length)")
    (tmp_path / "evidence_dispatch.inc").write_text(schema + "\n" + lookup + dispatch, encoding="utf-8")
    prefix = ["wsl.exe", "-d", "Ubuntu", "--exec"] if use_wsl else []
    exe = tmp_path / ("evidence_commit" if use_wsl else "evidence_commit.exe")
    subprocess.run([*prefix, "g++" if use_wsl else compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-I", host_path(gateway), "-I", host_path(tmp_path),
                    host_path(root / "tests/cpp/test_evidence_commit_flow.cpp"),
                    host_path(gateway / "vfence_protocol.cpp"), "-o", host_path(exe)], check=True, timeout=45)
    with subprocess.Popen([*prefix, host_path(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()


async def test_five_firmware_proofs_commit_and_advance_fpga(evidence_commit_host, tmp_path, monkeypatch):
    import app.fpga_service as worker

    process = evidence_commit_host
    output = tmp_path / "five-proofs.db"
    url = f"sqlite:///{output}"
    db = Database(url)
    db.initialize()
    config = SimpleNamespace(mock_serial=False, serial_port="test", windows_base=True,
                             central_sync_enabled=False, metrics_serial_ack_enabled=True,
                             metrics_gateway_id="gw", metrics_installation_id="site")
    reader = SerialReader(config, db, RecordingHub())
    reader._connection = FakeByteConnection()
    reader._connection.write_timeout = None
    service = FpgaService(config, db, reader)
    now = start = time.monotonic()
    session = "0000000000001234"
    report = dict(gatewaySessionId=session, activeProfileId=0, operationId=0, state="IDLE")
    network = NetworkWindow(session, 9, 160000, start, 0.01)
    count = 0
    commands = []

    async def read():
        return await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)

    def deliver(wire):
        process.stdin.write(wire)
        process.stdin.flush()

    async def gateway():
        nonlocal count
        if count == 5 or now < start + (count + 1) * 60 + 1:
            return report
        frame = await read()
        assert await reader.process_line(frame)
        ack = reader._connection.writes[-1]
        data = json.loads(ack[6:])
        assert data["command"] == "EVIDENCE_COMMITTED" and len(Database(url).proven_windows(1, 10)) == count + 1
        if not count:
            for field, bad in {"gatewaySessionId": "0000000000000001", "collarSessionId": "0000000000000002",
                               "windowId": "0000000000000008", "collarId": 2}.items():
                deliver(b"[CMD] " + json.dumps({**data, field: bad}, separators=(",", ":")).encode() + b"\n")
                assert await read() == b"REJECTED\n"
            assert not await reader.process_line(frame.replace(b'"hdopX100":101', b'"hdopX100":102'))
            assert reader._connection.writes == [ack]
            repeated = await read()
            assert repeated == frame and await reader.process_line(repeated)
            duplicate = reader._connection.writes[-1]
            resent = json.loads(duplicate[6:])
            assert resent == {**data, "requestId": data["requestId"] + 1}
            ack = duplicate
        deliver(ack)
        assert await read() == f"CONSUMED {9 + count}\n".encode()
        count += 1
        return report

    async def clock_probe(_session):
        return network

    async def pause(seconds):
        nonlocal now
        now += seconds

    async def jtag(line, expected, **_):
        commands.append(line)
        if line.startswith("SAMPLE "):
            assert line.split()[2] == str(count - 1) and line.endswith(" 2 15")
            return "OK|SAMPLE"
        if line == "ACK":
            with Database(url).connect() as con:
                assert con.execute("SELECT acknowledged FROM windows_fpga_results").fetchone()[0] == 0
        return {"BEGIN 0": "OK|BEGIN", "PING": "PONG", "CLOSE": "VFENCE|P0|0|0", "ACK": "OK|ACK"}[line]

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now))
    service.gateway, service.network_window, service.pause, service.command = gateway, clock_probe, pause, jtag
    await service.evaluate([1], {}, report, network=network, require_proven=True)
    assert await asyncio.to_thread(process.wait, 5) == 0
    assert count == 5 and len(reader._connection.writes) == 6
    assert len([line for line in commands if line.startswith("SAMPLE ")]) == 5
    assert commands[-2:] == ["CLOSE", "ACK"] and service.status["result"]["ack"]


@pytest.fixture
def firmware_frame(tmp_path):
    captured = os.getenv("VFENCE_WSL_EVIDENCE")
    if captured is not None:
        return captured.strip().encode("ascii") + b"\n"
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("requires C++ compiler or frame emitted by the WSL firmware test")
    root = Path(__file__).resolve().parents[1]
    gateway = root / "gateway" / "VFenceGateway"
    binary = tmp_path / "metric_evidence"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(gateway),
                    str(root / "tests/cpp/test_metric_evidence.cpp"), str(gateway / "vfence_protocol.cpp"),
                    "-o", str(binary)], check=True)
    return subprocess.check_output([binary])


async def test_firmware_evidence_survives_reopen_and_feeds_aligned_fpga_sample(firmware_frame, tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'evidence.db'}"
    database = Database(url)
    database.initialize()
    config = SimpleNamespace(mock_serial=False, metrics_gateway_id="gw", metrics_installation_id="site",
                             metrics_serial_ack_enabled=True, central_sync_enabled=False, serial_port="test")
    reader = SerialReader(config, database, RecordingHub())
    reader._connection = object()
    sent = []

    async def committed(connection, command, **fields):
        assert connection is reader._connection
        # The frame's projection must already be visible on a separate connection.
        saved = Database(url).fpga_window_metrics(2, 10)
        assert saved["pdr"][0]["pdr_x100"] == 8000 and saved["pdr"][0]["tx_proven"] == 1
        sent.append((command, fields))

    monkeypatch.setattr(reader, "_send_serial_commit_command", committed)
    assert await reader.process_line(firmware_frame)
    assert await reader.process_line(firmware_frame)  # lost commit response
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0][0] == "EVIDENCE_COMMITTED"
    assert not await reader.process_line(firmware_frame.replace(b'"pdrX100":8000', b'"pdrX100":10000'))
    assert not await reader.process_line(firmware_frame.replace(b'"hdopX100":110', b'"hdopX100":111'))
    assert len(sent) == 2
    service = FpgaService(config, Database(url), reader)
    records = await asyncio.to_thread(service.records, [2])
    record = records[2]["pdr"][0]
    batch = FpgaWindow("gw", record["gateway_session_id"], 160000, 1,
                       {2: record["collar_session_id"]}, time.monotonic() + 30)
    service.samples(batch, records, {}, lambda row: row["window_id"] == record["window_id"])
    assert len(batch.ready_samples()) == 1
    assert sample_command(batch.ready_samples()[0]) == "SAMPLE 2 0 8000 600 94 110 8 15"


async def test_operational_window_sends_firmware_frame_without_manual_clock_map(firmware_frame, monkeypatch, tmp_path):
    import app.fpga_service as worker
    from app import main

    url = f"sqlite:///{tmp_path / 'integrated.db'}"
    database = Database(url)
    database.initialize()
    config = SimpleNamespace(mock_serial=False, metrics_gateway_id="gw", metrics_installation_id="site",
                             central_sync_enabled=False, serial_port="test", windows_base=True,
                             metrics_serial_ack_enabled=True)
    reader = SerialReader(config, database, RecordingHub())
    reader.state.status = "online"
    reader.effective_profile = {"profile_id": 1, "observed_at": worker._utc_now()}

    class Wire(FakeByteConnection):
        write_timeout = 2

        def write(self, wire):
            report = Database(url).proven_windows(2, 1)[0]
            assert report["pdr"]["transmitted_count"] == 10 and report["pdr"]["received_count"] == 8
            return super().write(wire)

    reader._connection = Wire()
    service = FpgaService(config, database, reader)
    start = time.monotonic()
    now = start
    session = "0000000000001234"
    network = NetworkWindow(session, 9, 160000, start, 0.05)
    report = dict(gatewaySessionId=session, operationId=0, activeProfileId=1, state="COMPLETED")
    commands = []

    async def gateway():
        nonlocal now
        if now == start:
            now += 62
            assert await reader.process_line(firmware_frame)
        return report

    async def same_clock(_session):
        return network

    async def command(line, expected, **kwargs):
        commands.append(line)
        if line == "ACK":
            with Database(url).connect() as db:
                decision = db.execute("SELECT result_json FROM windows_fpga_results").fetchone()
            assert json.loads(decision[0])["wire"] == "VFENCE|P1|4|0"
        return {"BEGIN 1": "OK|BEGIN", "SAMPLE 2 0 8000 600 94 110 8 15": "OK|SAMPLE", "PING": "PONG",
                "CLOSE": "VFENCE|P1|4|0", "ACK": "OK|ACK"}[line]

    async def pause(_seconds):
        nonlocal now
        now += 260

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(service, "gateway", gateway)
    monkeypatch.setattr(service, "network_window", same_clock)
    monkeypatch.setattr(service, "command", command)
    monkeypatch.setattr(service, "pause", pause)
    await service.evaluate([2], {}, report, network=network, require_proven=True)
    assert commands == ["BEGIN 1", "SAMPLE 2 0 8000 600 94 110 8 15", "PING", "CLOSE", "ACK"]
    serial_ack, = reader._connection.writes
    serial_ack = json.loads(serial_ack.split(b"[CMD] ", 1)[1])
    assert serial_ack["command"] == "EVIDENCE_COMMITTED" and serial_ack["collarId"] == 2
    assert serial_ack["gatewaySessionId"] == session and serial_ack["windowId"] == "0000000000000009"
    with Database(url).connect() as db:
        assert db.execute("SELECT acknowledged FROM windows_fpga_results").fetchone()[0] == 1
    for key, val in {"database": database, "reader": reader, "fpga_service": service, "settings": config}.items():
        monkeypatch.setattr(main, key, val)
    service._last_network_clock = (network, now, reader.serial_generation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        pdr = (await client.get("/api/collars/2/pdr")).json()[0]
        windows = (await client.get("/api/collars/2/link-metrics")).json()["windows"]
        assert pdr["tx_proven"] is True and pdr["pdr_x100"] == 8000
        assert windows["radio"][0]["rssi_mean_x100"] == -9400 and windows["radio"][0]["snr_mean_x100"] == 600
        assert windows["hdop"][0]["hdop_mean_x100"] == 110 and windows["hdop"][0]["sample_count"] == 2
        assert len({rows[0]["window_id"] for rows in windows.values()}) == 1
        assert all(rows[0]["freshness"] == "historical" for rows in windows.values())
        assert (await client.get("/api/fpga/collars")).json()[0]["collar_id"] == 2
        status = (await client.get("/api/fpga/status")).json()
        assert status["result"]["ack"] is True
        assert status["last_persisted_result"]["ack"] is True
        assert status["last_persisted_result"]["wire"] == "VFENCE|P1|4|0"


@pytest.mark.parametrize(("tx", "rx", "ratio"), [
    (10, 10, 10000), (10, 8, 8000), (10, 5, 5000), (10, 0, 0), (1, 1, 10000), (0, 0, None),
])
def test_validated_pdr_denominator_is_distinct_from_voting_minimum(firmware_frame, tx, rx, ratio):
    data = json.loads(firmware_frame[6:])
    data.update(txFirst=0xfffffffc, txLast=(0xfffffffc + tx) & 0xffffffff, receivedCount=rx,
                pdrX100=ratio, radioCount=0, rssiX100=None, snrX100=None)
    summary = parse_evidence(data)
    pdr = summary.project("gw", "site", "2026-01-01T00:00:00Z")["pdr"]
    assert (pdr["transmitted_count"], pdr["pdr_x100"], pdr["valid"]) == (tx, ratio, tx != 0)
    from app.fpga_service import _record
    from app.pdr_protocol import PdrSummary
    batch = FpgaWindow("gw", summary.gateway_session_id, summary.started_at_ms, summary.profile_id,
                       {summary.collar_id: summary.collar_session_id}, time.monotonic() + 10)
    assert batch.add(gateway_id="gw", pdr=_record(PdrSummary, pdr))
    assert batch.ready_samples()[0].pdr_x100 == (ratio if rx >= 2 else None)


@pytest.mark.parametrize("overrides", [
    {"txLast": None}, {"receivedCount": 11}, {"pdrX100": 9999}, {"radioCount": 9},
    {"radioCount": 0}, {"hdopCount": 0}, {"profileId": True},
])
def test_bad_evidence_cannot_produce_proven_tx(firmware_frame, overrides):
    data = json.loads(firmware_frame[6:])
    with pytest.raises(ValueError):
        parse_evidence({**data, **overrides})


async def test_sqlite_failure_prevents_evidence_ack(firmware_frame, tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'no-schema.db'}")
    config = SimpleNamespace(mock_serial=False, metrics_gateway_id="gw", metrics_installation_id="site",
                             central_sync_enabled=False, serial_port="test", metrics_serial_ack_enabled=True)
    hub = RecordingHub()
    reader = SerialReader(config, db, hub)
    reader._connection = FakeByteConnection()
    assert not await reader.process_line(firmware_frame)
    assert reader._connection.writes == [] and hub.messages == []
