import asyncio
import logging
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import serial

from app.database import Database
from app.serial_reader import SerialReader
from app.terminal import ALERT_RULE, BLOCK_RULE, base_value
from test_metric_serial_export import MetricConnection, ack, running, until
from test_metric_summaries import RecordingHub, hdop_json, radio_json, serial_line
from vfence_pc_serial import VFenceSerialClient


def telemetry(collar_id, *, valid=True):
    return {
        "schema": "vfence.gateway.serial.v1", "kind": "telemetry", "collarId": collar_id,
        "sequence": 27, "latitude": -31.0 if valid else None, "longitude": -54.0 if valid else None,
        "gpsValid": valid, "inside": valid, "near": False, "veryNear": False,
        "hdop": 1.42 if valid else None, "rssi": -93, "snr": -3.75,
    }


def pc(detail=False):
    with patch("vfence_pc_serial.serial.Serial"):
        client = VFenceSerialClient("test", terminal_detail=detail)
    client.ser.write.side_effect = len
    return client


@pytest.fixture
def framed_gateway(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("compilador C++ de host indisponivel")
    root = Path(__file__).resolve().parents[1]
    gateway = root / "gateway/VFenceGateway"
    executable = tmp_path / "terminal_serial"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(gateway),
                    str(root / "tests/cpp/test_terminal_serial.cpp"), "-o", str(executable)], check=True)
    return executable


def test_firmware_terminal_writer_is_bounded(framed_gateway):
    subprocess.run([framed_gateway], check=True)


async def test_gateway_short_writes_recover_in_base_parser(framed_gateway):
    wire = subprocess.check_output([framed_gateway, "wire"])
    database = Database("sqlite:///:memory:")
    database.initialize()
    reader = SerialReader(SimpleNamespace(mock_serial=False, serial_port="test",
                                          central_sync_enabled=False), database, RecordingHub())
    response = asyncio.get_running_loop().create_future()
    reader._pending_commands[17] = response
    log_fragment, wire = wire.split(b"\0", 1)
    await reader._process_serial_chunk(log_fragment + b"\0")
    await reader._process_serial_chunk(b"")
    assert not response.done()
    first_response, position = wire.split(b"\r\n", 1)
    await reader._process_serial_chunk(first_response + b"\r\n")
    assert response.result()["command"] == "GET_PROFILE"

    lost_lf, position = position.split(b"\0", 1)
    await reader._process_serial_chunk(lost_lf)
    await reader._process_serial_chunk(b"")
    assert database.telemetry("COL07") == []
    await reader._process_serial_chunk(b"\0" + position)
    assert len(database.telemetry("COL07")) == 1


async def test_short_get_profile_waits_for_framing_and_accepts_next_reply():
    database = Database("sqlite:///:memory:")
    database.initialize()
    reader = SerialReader(SimpleNamespace(mock_serial=False, serial_port="test",
                                          central_sync_enabled=False), database, RecordingHub())
    loop = asyncio.get_running_loop()
    get_profile, get_radio = loop.create_future(), loop.create_future()
    reader._pending_commands.update({1: get_profile, 2: get_radio})
    profile = {"schema": "vfence.gateway.serial.v1", "kind": "command_response",
               "command": "GET_PROFILE", "requestId": 1, "ok": True}
    radio = {**profile, "command": "GET_RADIO", "requestId": 2}
    await reader._process_serial_chunk(serial_line(profile, b"\r"))
    await reader._process_serial_chunk(b"")
    assert not get_profile.done()
    await reader._process_serial_chunk(b"\0")
    await reader._process_serial_chunk(b"")
    await reader._process_serial_chunk(b"\n" + serial_line(radio))
    assert not get_profile.done() and get_radio.result()["ok"] is True
    await reader._process_serial_chunk(serial_line(profile, b"\r\n"))
    assert get_profile.result()["ok"] is True


@pytest.mark.parametrize("collar_id", [1, 2, 7, 254])
def test_pc_position_identifies_packet_and_shows_missing_metrics(capsys, collar_id):
    client = pc()
    client._handle_app_message(telemetry(collar_id))
    invalid = telemetry(collar_id, valid=False)
    invalid.pop("snr")
    client._handle_app_message(invalid)
    first, second = capsys.readouterr().out.splitlines()
    assert f"[COL {collar_id:03d}] seq=27" in first
    assert "GPS=VALIDO HDOP=1.42 RSSI=-93dBm SNR=-3.75dB ZONA=SEGURO" in first
    assert "GPS=INVALIDO HDOP=N/A RSSI=-93dBm SNR=N/A ZONA=N/A" in second


def test_pc_keeps_window_origins_and_never_confirms_persistence(capsys):
    client = pc()
    client._handle_app_message(hdop_json(collarId=7))
    client._handle_app_message(radio_json(collarId=254, sampleCount=0, rssiMeanX100=None,
                                        snrMeanX100=None, rssiMeanValid=False, snrMeanValid=False))
    client._handle_app_message({"kind": "event", "subtype": "OUTSIDE", "collarId": 2, "sequence": 42})
    client._handle_app_message({"kind": "fence_transfer", "collarId": 1, "fenceVersion": 3,
                                "requestId": 8, "state": "confirmed"})
    output = capsys.readouterr().out
    assert "HDOP 60s | COL 007" in output and "RADIO 60s | COL 254" in output
    assert "Sessao: ffffffffffffffff | Janela: fffffffffffffffe" in output
    assert "Sessao: 8000000000000000 | Janela: fffffffffffffffe" in output
    assert "HDOP medio: 1.42" in output and "RSSI medio: N/A | SNR medio: N/A" in output
    assert "[ALERTA] [PC / GATEWAY] [COL 002] OUTSIDE seq=42" in output
    assert "requestId=8 estadoGateway=confirmed" in output
    assert "COMMIT concluido" not in output
    client.ser.write.assert_not_called()


def test_pc_detail_keeps_original_command_bytes_and_raw_output(capsys):
    client = pc(detail=True)
    client.send("PING", collarId=7, token=91)
    line = b'[CMD] {"schema":"vfence.gateway.serial.v1","requestId":1,"command":"PING","collarId":7,"token":91}\n'
    client.ser.write.assert_called_once_with(line)
    assert "[RAW PC] " + line.decode().strip() in capsys.readouterr().out


@pytest.mark.parametrize("command", ["METRIC_COMMITTED", "  metric_committed\r\n", "ＭＥＴＲＩＣ＿ＣＯＭＭＩＴＴＥＤ"])
def test_pc_blocks_reserved_ack_without_writing(command):
    client = pc()
    with pytest.raises(ValueError, match="exclusivo da Base apos COMMIT"):
        client.send(command, collarId=1, sessionId="0000000000000001")
    client.ser.write.assert_not_called()
    client.ser.flush.assert_not_called()
    assert client.send("GET_STATUS") == 1


def test_pc_rejects_short_write_io_error_and_recovers_delimiter(capsys):
    client = pc()
    client.ser.write.side_effect = [9, serial.SerialTimeoutException("timeout")]
    with pytest.raises(OSError, match="escrita incompleta"):
        client.send("GET_STATUS")
    with pytest.raises(serial.SerialTimeoutException, match="timeout"):
        client.send("GET_RADIO")
    client.ser.flush.assert_not_called()
    output = capsys.readouterr().out
    assert output.count("[ERRO SERIAL] [PC]") == 2 and "[TX SERIAL]" not in output

    client.ser.write.side_effect = len
    assert client.send("FENCE_NEW", collarId=2, fenceVersion=3, vertexCount=4) == 3
    assert client.ser.write.call_args.args[0] == (
        b'\x00\n[CMD] {"schema":"vfence.gateway.serial.v1","requestId":3,"command":"FENCE_NEW",'
        b'"collarId":2,"fenceVersion":3,"vertexCount":4}\n'
    )
    client.send("FENCE_SEND")
    assert client.ser.write.call_args.args[0].startswith(b"[CMD] ")
    assert "[TX SERIAL]" in capsys.readouterr().out


def test_pc_bounds_and_recovers_rx_framing_in_normal_mode(capsys):
    client = pc()
    row = serial_line(telemetry(254)).removesuffix(b"\n") + b"\r\n"
    client.ser.read_until.side_effect = [
        b"[APP] []\n", b"[APP] 42\n", b"[APP] {malformado}\n",
        b"[RX] pacote invalido: header/tamanho\n", b"[RX LoRa] pacote maior que MAX_PACKET_SIZE: 99\n",
        b"[RX LoRa] [GATEWAY] bytes=2\n", b"DE AD\r\n",
        b"x" * 4097, serial_line(telemetry(7)),  # Sufixo da linha longa. O LF apenas ressincroniza.
        b"[APP] \"cortado\"\x00\n", row[:20], row[20:],
        serial_line(telemetry(2)).rstrip(b"\n"), serial.SerialException("USB removida"),
    ]
    client._running = True
    client._reader_loop()
    output = capsys.readouterr().out
    assert output.count("[ERRO JSON] [PC]") == 3
    assert "linha excede 4096" in output and "enquadramento invalido" in output
    assert "[RX] pacote invalido: header/tamanho" in output and "pacote maior" in output
    assert "DE AD" not in output and "bytes=2" not in output
    assert client.known_collars() == {254: "ONLINE"}
    assert "[COL 254] seq=27" in output and "[COL 002]" not in output
    assert "USB removida" in output and not client._running
    assert client.ser.read_until.call_args_list[0].args == (b"\n", 4097)


def test_pc_restart_reader_discards_old_fragment_and_resyncs_tx(capsys):
    client = pc()
    client.ser.close.side_effect = lambda: setattr(client.ser, "is_open", False)
    client.ser.open.side_effect = lambda: setattr(client.ser, "is_open", True)
    client.ser.read_until.side_effect = [b"[APP] {\"kind\":", serial.SerialException("caiu")]
    client.start()
    client._reader.join(timeout=1)
    assert not client._reader.is_alive()
    client.ser.read_until.side_effect = [serial_line(telemetry(1)), serial.SerialException("fim")]
    client.start()
    client._reader.join(timeout=1)
    client.ser.open.assert_called_once()
    assert client.known_collars() == {1: "ONLINE"}
    assert "[ERRO JSON]" not in capsys.readouterr().out
    client.ser.is_open = True
    client.send("GET_RADIO")
    assert client.ser.write.call_args.args[0] == b'\x00\n[CMD] {"schema":"vfence.gateway.serial.v1","requestId":1,"command":"GET_RADIO"}\n'


async def test_base_warns_on_persisted_transition_once_and_keeps_lora_event_separate(tmp_path, caplog):
    db = Database(f"sqlite:///{tmp_path / 'transitions.db'}")
    db.initialize()
    hub = RecordingHub()
    config = SimpleNamespace(mock_serial=False, serial_port="test", central_sync_enabled=False,
                             max_line_length=512)
    reader = SerialReader(config, db, hub)
    safe = telemetry(2)
    outside = {**safe, "inside": False, "sequence": 28}
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        await reader.process_line(serial_line(safe))
        await reader.process_line(serial_line(outside))
        await reader.process_line(serial_line({**outside, "sequence": 29}))
        await reader.process_line(serial_line({**safe, "sequence": 30}))
        await reader.process_line(serial_line({"schema": safe["schema"], "kind": "event", "subtype": "OUTSIDE",
                                               "collarId": 2, "sequence": 31}))
    assert caplog.text.count("ALERTA | TRANSICAO DE ZONA") == 1
    assert "SEGURO -> FORA" in caplog.text and "FORA -> SEGURO" in caplog.text
    assert "Origem: telemetria persistida" in caplog.text and "[COL 002] TRANSICAO DE ZONA" in caplog.text
    assert "[BASE][COL 002] ALERTA | OUTSIDE" in caplog.text
    assert len(db.telemetry("COL02")) == 4
    assert sum(item["new_value"] == "FORA" for item in db.events()) == 1
    assert [message["type"] for message in hub.messages] == ["telemetry"] * 4 + ["gateway_event"]


async def test_base_output_is_compact_and_distinguishes_commit_and_event(tmp_path, caplog):
    db = Database(f"sqlite:///{tmp_path / 'terminal.db'}")
    db.initialize()
    config = SimpleNamespace(mock_serial=False, serial_port="test", central_sync_enabled=False,
                             metrics_installation_id="TEST", metrics_gateway_id="TEST", max_line_length=512)
    reader = SerialReader(config, db, RecordingHub())
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        await reader.process_line(serial_line(telemetry(1)))
        await reader.process_line(serial_line(hdop_json(collarId=2)))
        await reader.process_line(serial_line(hdop_json(collarId=2)))
        await reader.process_line(serial_line(hdop_json(collarId=2, hdopMeanX100=15)))
        await reader.process_line(serial_line(radio_json(collarId=254)))
        await reader.process_line(serial_line({"schema": "vfence.gateway.serial.v1", "kind": "event",
                                               "subtype": "GPS_LOST", "collarId": 7}))
    logs = "\n".join(record.message for record in caplog.records)
    assert "[BASE][COL 001] POSICAO | SQLite: SALVA" in logs
    assert "[APP]" not in logs
    assert "SQLite: INSERTED | COMMIT CONCLUIDO" in logs
    assert "[COL 002] HDOP | JANELA 60s" in logs and "DUPLICATE" in logs and "CONFLICT" in logs
    assert "[COL 254] RADIO | JANELA 60s" in logs and "RSSI medio: -93.40 dBm | SNR medio: -3.75 dB" in logs
    assert "[BASE][COL 007] ALERTA | GPS_LOST" in logs
    assert "ACK ENFILEIRADO" not in logs and "Escrita Serial: CONCLUIDA" not in logs
    assert len(db.telemetry("COL01")) == 1


@pytest.mark.parametrize("short_write", [False, True])
async def test_base_reports_commit_queue_and_local_ack_in_order(tmp_path, caplog, short_write):
    db = Database(f"sqlite:///{tmp_path / 'terminal_ack.db'}")
    db.initialize()
    config = SimpleNamespace(
        mock_serial=False, serial_port="test", serial_baud=115200, reconnect_seconds=0,
        central_sync_enabled=False, metrics_serial_ack_enabled=True,
        metrics_installation_id="TEST", metrics_gateway_id="TEST",
        terminal_detail=True,
    )
    reader = SerialReader(config, db, RecordingHub())
    port = MetricConnection(serial_line(hdop_json(collarId=7)))
    real_write = port.write
    lock_states = []

    class AckObserver(logging.Handler):
        def emit(self, record):
            if "Escrita Serial:" in record.getMessage():
                lock_states.append(reader._write_lock.locked())

    observer = AckObserver()
    reader_logger = logging.getLogger("serial_reader")
    reader_logger.addHandler(observer)
    outcome = "FALHOU" if short_write else "CONCLUIDA"
    try:
        with patch.object(port, "write", side_effect=lambda line: real_write(line) - int(short_write)), \
                caplog.at_level(logging.INFO, logger="serial_reader"):
            async with running(reader, port):
                await until(lambda: f"Escrita Serial: {outcome}" in caplog.text)
    finally:
        reader_logger.removeHandler(observer)
    logs = caplog.text
    assert logs.index("COMMIT CONCLUIDO") < logs.index("ACK ENFILEIRADO") < logs.index(f"Escrita Serial: {outcome}")
    assert lock_states == [False]
    if short_write:
        assert "Escrita Serial: CONCLUIDA" not in logs
    assert "ACK CONSUMIDO" not in logs
    assert len(port.writes) == 1 and b'"command":"METRIC_COMMITTED"' in port.writes[0]


def base_reader(**overrides):
    db = Database("sqlite:///:memory:")
    db.initialize()
    config = SimpleNamespace(
        mock_serial=False, serial_port="test", serial_baud=115200, reconnect_seconds=0,
        central_sync_enabled=False, metrics_installation_id="TEST", metrics_gateway_id="TEST",
        max_line_length=512,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return SerialReader(config, db, RecordingHub()), db


async def test_slow_position_log_keeps_burst_alert_and_ack_consistent(caplog):
    reader, db = base_reader(metrics_serial_ack_enabled=True)
    port = MetricConnection(serial_line(telemetry(1)))
    alert = {"schema": "vfence.gateway.serial.v1", "kind": "event", "collarId": 2, "subtype": "OUTSIDE"}
    lock_states = []
    burst_sent = False

    class SlowSink(logging.Handler):
        def emit(self, record):
            nonlocal burst_sent
            line = record.getMessage()
            if not burst_sent and "POSICAO | SQLite: SALVA" in line:
                burst_sent = True
                port.feed(*(serial_line(telemetry(collar)) for collar in range(3, 9)),
                          serial_line(alert), serial_line(hdop_json(collarId=2)))
                time.sleep(0.05)
            if "Escrita Serial: CONCLUIDA" in line:
                lock_states.append(reader._write_lock.locked())

    logger = logging.getLogger("serial_reader")
    sink = SlowSink()
    logger.addHandler(sink)
    try:
        with caplog.at_level(logging.INFO, logger="serial_reader"):
            async with running(reader, port):
                await until(lambda: "Escrita Serial: CONCLUIDA" in caplog.text and "ALERTA | OUTSIDE" in caplog.text)
    finally:
        logger.removeHandler(sink)
    assert caplog.text.count("POSICAO | SQLite: SALVA") == 7
    assert caplog.text.count("ALERTA | OUTSIDE") == 1
    assert caplog.text.index("COMMIT CONCLUIDO") < caplog.text.index("Escrita Serial: CONCLUIDA")
    assert len(port.writes) == 1 and ack(port.writes[0])["collarId"] == 2
    assert len(db.telemetry("COL08")) == 1 and lock_states == [False]


@pytest.mark.parametrize("detail", [False, True])
async def test_base_position_blocks_remain_independent_during_concurrent_packets(caplog, detail):
    reader, db = base_reader(terminal_detail=detail)
    frames = [
        telemetry(1), {**telemetry(2, valid=False), "rssi": None, "snr": None}, telemetry(254),
        {**telemetry(2), "sequence": 28},
    ]
    with caplog.at_level(logging.DEBUG, logger="serial_reader"):
        await asyncio.gather(*(reader.process_line(serial_line(frame)) for frame in frames))
    records = [record.getMessage() for record in caplog.records if "POSICAO | SQLite: SALVA" in record.getMessage()]
    assert len(records) == 4
    assert sorted(re.findall(r"\[COL (\d{3})\]", "\n".join(records))) == ["001", "002", "002", "254"]
    for block in records:
        lines = block.splitlines()
        assert lines[1] == lines[-1] == BLOCK_RULE
        assert len(lines) == (7 if detail else 6)
        assert len(re.findall(r"\[COL (\d{3})\]", block)) == 1
    invalid = next(block for block in records if "GPS: INDISPONIVEL" in block)
    assert "Zona: N/A | HDOP: N/A" in invalid and "RSSI: N/A | SNR: N/A" in invalid
    assert ("[RAW POS]" in caplog.text) == detail
    assert ("Sequencia: 28" in caplog.text) == detail
    assert len(db.telemetry("COL02")) == 2


@pytest.mark.parametrize("detail", [False, True])
async def test_base_metric_blocks_separate_windows_and_preserve_invalidity(caplog, detail):
    reader, db = base_reader(terminal_detail=detail)
    pdr = {
        "schema": "vfence.gateway.pdr.v1", "kind": "pdr_window_summary", "collarId": 254,
        "gatewaySessionId": "8000000000000000", "windowId": "fffffffffffffffe",
        "collarSessionId": "0000000000000009", "startedAtMs": 120000, "durationMs": 60000,
        "profileId": 0, "receivedCount": 2, "estimatedTxCount": 3, "estimatedLostCount": 1,
        "pdrX100": 6667, "valid": True, "reason": "ok",
    }
    frames = [
        hdop_json(), hdop_json(collarId=2, sampleCount=0, hdopMeanX100=None, hdopMeanValid=False),
        radio_json(), radio_json(collarId=2, sampleCount=0, rssiMeanX100=None, snrMeanX100=None,
                                rssiMeanValid=False, snrMeanValid=False),
        pdr, {**pdr, "collarId": 2, "collarSessionId": None, "estimatedTxCount": None,
              "estimatedLostCount": None, "pdrX100": None, "valid": False, "reason": "session_changed"},
    ]
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        for frame in frames:
            await reader.process_line(serial_line(frame))
    blocks = [record.getMessage() for record in caplog.records if "JANELA 60s" in record.getMessage()]
    assert len(blocks) == 6 and all(block.splitlines()[1] == block.splitlines()[-1] == BLOCK_RULE for block in blocks)
    hdop, empty_hdop, radio, empty_radio, measured, invalid_pdr = blocks
    assert "[COL 001] HDOP" in hdop and "HDOP medio: 1.42" in hdop
    assert "[COL 002] HDOP" in empty_hdop and "SEM AMOSTRAS" in empty_hdop
    assert "[COL 001] RADIO" in radio and "RSSI medio: -93.40 dBm | SNR medio: -3.75 dB" in radio
    assert "[COL 002] RADIO" in empty_radio and "RSSI medio: N/A | SNR medio: N/A" in empty_radio
    assert "[COL 254] PDR ESTIMADO" in measured and "Entrega: 66.67% | Perfil: P0" in measured
    assert "Recebidos: 2 | Intervalo estimado: 3" in measured
    assert "Entrega: N/A" in invalid_pdr and "Motivo: session_changed" in invalid_pdr
    assert ("Inicio (coleira)" in hdop) == detail and ("Inicio (Gateway)" in radio) == detail
    assert ("Sessao coleira:" in measured) == detail
    assert "ACK | PDR" not in caplog.text and "ACK ENFILEIRADO" not in caplog.text
    assert db.list_pdr(254, 1)[0]["pdr_x100"] == 6667

    observations = [record.getMessage() for record in caplog.records if "DADOS RECEBIDOS" in record.getMessage()]
    assert len(observations) == 6
    hdop, empty_hdop, radio, empty_radio, measured, invalid_pdr = observations
    assert "hdopMeanX100=142 | hdopMeanValid=True" in hdop
    assert "gatewayReceivedAtMs=1250" in hdop
    assert "hdop_x100=DISPONIVEL" in hdop and "pdr_x100=AUSENTE" in hdop
    assert "hdop_x100=INVALIDO" in empty_hdop and "sampleCount=0" in empty_hdop
    assert "rssiMeanX100=-9340 | rssiMeanValid=True" in radio and "snr_x100=DISPONIVEL" in radio
    assert "rssi_mag=INVALIDO" in empty_radio and "sample_count=INVALIDO" in empty_radio
    assert "pdrX100=6667" in measured and "estimatedLostCount=1" in measured
    assert "pdr_x100=INVALIDO" in invalid_pdr and "reason=session_changed" in invalid_pdr
    for observation in observations:
        assert all(marker in observation for marker in (
            "Sessao=", "Janela=", "RX Base (UTC)=", "Duracao=60000 ms", "PREVIA FPGA",
            "NAO ENVIADO — INTEGRACAO PENDENTE",
        ))
        assert "ENVIADO AO FPGA" not in observation


async def test_fpga_diagnostic_skips_duplicates_and_rejected_packets(caplog):
    reader, db = base_reader()
    frame = radio_json(snrMeanX100=0, sampleCount=256)
    rejected = hdop_json(hdopMeanX100=65535)
    del rejected["windowId"]
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        assert await reader.process_line(serial_line(frame))
        assert await reader.process_line(serial_line(frame))
        assert not await reader.process_line(serial_line(rejected))
        db.link_metrics(1, 1)
        db.link_metrics(1, 1)
    assert caplog.text.count("DADOS RECEBIDOS") == 1
    assert caplog.text.count("PREVIA FPGA") == 1
    assert "snrMeanX100=0" in caplog.text and "snr_x100=DISPONIVEL" in caplog.text
    assert "sample_count=INVALIDO" in caplog.text
    assert "rssi_mag=DISPONIVEL" in caplog.text and "hdop_x100=AUSENTE" in caplog.text
    assert "DUPLICATE | REGISTRO IDENTICO JA PERSISTIDO" in caplog.text
    assert "gateway application line rejected" in caplog.text


async def test_base_sqlite_error_does_not_report_position_saved(caplog):
    reader, db = base_reader(reconnect_seconds=0.3)
    port = MetricConnection(serial_line(telemetry(1)))
    with patch.object(db, "save_packet", side_effect=sqlite3.OperationalError("simulated disk error")):
        async with running(reader, port):
            await until(lambda: "LEITURA INTERROMPIDA POR FALHA NO SQLITE" in caplog.text)
    assert "POSICAO | SQLite: SALVA" not in caplog.text
    assert "CONEXAO SERIAL INTERROMPIDA" not in caplog.text
    assert db.telemetry("COL01") == []


async def test_base_does_not_print_position_saved_while_sqlite_is_pending(caplog):
    reader, db = base_reader()
    saving, release = threading.Event(), threading.Event()
    save_packet = db.save_packet

    def blocked_save(*args):
        saving.set()
        assert release.wait(5)
        return save_packet(*args)

    with patch.object(db, "save_packet", side_effect=blocked_save), caplog.at_level(logging.INFO):
        task = asyncio.create_task(reader.process_line(serial_line(telemetry(1))))
        try:
            await until(saving.is_set)
            assert "POSICAO | SQLite: SALVA" not in caplog.text
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
    assert "POSICAO | SQLite: SALVA" in caplog.text
    assert len(db.telemetry("COL01")) == 1


async def test_base_detailed_raw_is_bounded_and_keeps_control_characters_off_terminal(caplog):
    reader, _ = base_reader(terminal_detail=True)
    controls = "\x1b\x85\u061c\u200e\u200f\u2028\u2029\u202a\u202e\u2066\u2069"
    with caplog.at_level(logging.INFO):
        await reader.process_line("Gateway: " + "A" * 100 + controls + "B" * 1000)
        reader._diagnostic("[RAW TEST] %s", "ok" + controls + "B" * 1000)
    human, bounded = [record.getMessage() for record in caplog.records]
    assert human == f"[RAW GATEWAY] Linha de diagnostico | Caracteres: {1109 + len(controls)}"
    assert bounded.startswith("[RAW TEST] ok") and bounded.endswith("...")
    assert len(bounded.removeprefix("[RAW TEST] ")) == 515
    assert all(char not in caplog.text for char in controls)
    assert base_value("a" + controls + "b") == "a" + " " * len(controls) + "b"


async def test_base_raw_allowlist_keeps_payloads_for_application_without_exposing_credentials(caplog):
    reader, db = base_reader(terminal_detail=True)
    secrets = {
        "token": "example-token-omit", "Password": "example-password-omit",
        "sEcReT": "example-secret-omit", "AUTHORIZATION": "example-authorization-omit",
        "Cookie": "example-cookie-omit", "access_token": "example-access-omit",
        "headers": [{"Set-Cookie": "example-nested-cookie-omit"}],
    }
    schema = {"schema": "vfence.gateway.serial.v1"}
    raw_position = {**telemetry(3), **secrets}
    raw_response = {
        **schema, **secrets, "kind": "command_response", "command": "GET_PROFILE", "requestId": 7,
        "collarId": 3, "ok": True, "state": "IDLE", "activeProfileId": 0, "operationId": 12,
        "detail": "profile_status", "sequence": {"Authorization": "example-nested-allowed-omit"},
        "profiles": [{"frequencyHz": 915000000, "token": "example-nested-profile-omit"}],
    }
    response = reader._pending_commands[7] = asyncio.get_running_loop().create_future()
    with caplog.at_level(logging.INFO):
        assert await reader.process_line(serial_line(raw_response))
        assert await reader.process_line(serial_line(raw_position))
        assert await reader.process_line(serial_line({
            **raw_response, "requestId": 8, "detail": "example-free-detail-omit\u202e",
            "command": "example-free-command-omit\x1b[31m",
        }))
        assert not await reader.process_line(serial_line({
            **telemetry(3), "sequence": "example-invalid-number-omit",
        }))
    assert response.result() == raw_response
    assert "example-token-omit" in db.telemetry("COL03")[0]["raw_message"]
    assert "example-" not in caplog.text
    assert "[RAW POS] [BASE] [COL 003]" in caplog.text and '"hdop":1.42' in caplog.text
    assert '"command":"GET_PROFILE"' in caplog.text and '"activeProfileId":0' in caplog.text
    assert "Detalhe: profile_status" in caplog.text
    assert "gateway application line rejected (ValueError)" in caplog.text


async def test_base_reports_usb_open_disconnect_and_reopen_separately(caplog):
    reader, _ = base_reader()
    first, second = MetricConnection(), MetricConnection()
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        async with running(reader, first, second):
            await until(lambda: "Estado: PORTA ABERTA" in caplog.text)
            first.feed(OSError("USB simulada desconectada"))
            await until(lambda: "Estado: PORTA REABERTA" in caplog.text)
    assert caplog.text.index("PORTA ABERTA") < caplog.text.index("CONEXAO SERIAL INTERROMPIDA")
    assert caplog.text.index("CONEXAO SERIAL INTERROMPIDA") < caplog.text.index("PORTA REABERTA")
    assert "GATEWAY RESPONDEU" not in caplog.text and first.closed and second.closed


async def test_base_startup_does_not_claim_usb_or_central_connected(monkeypatch, caplog):
    from app import main
    reader, db = base_reader(central_sync_enabled=True, metrics_serial_ack_enabled=True, terminal_detail=False)

    async def noop():
        pass

    monkeypatch.setattr(main, "settings", reader.config)
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "reader", SimpleNamespace(run=noop, stop=noop))
    monkeypatch.setattr(main, "sync_service", SimpleNamespace(run=noop, stop=noop))
    with caplog.at_level(logging.INFO, logger="server"):
        async with main.lifespan(main.app):
            pass
    assert "VFENCE | RASPBERRY PI | BASE LOCAL" in caplog.text
    assert "SQLite: INICIALIZADO | Central: SINCRONIZACAO CONFIGURADA" in caplog.text
    assert "Serial: test | Baud: 115200 | Estado: CONFIGURADA | AGUARDANDO ABERTURA" in caplog.text
    assert "PORTA ABERTA" not in caplog.text and "CICLO CONCLUIDO" not in caplog.text


async def test_base_gateway_events_keep_origin_and_distinct_highlight(caplog):
    reader, db = base_reader()
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        for name in ("OUTSIDE", "BACK_INSIDE", "GPS_LOST", "GPS_RECOVERED", "OFFLINE", "ONLINE"):
            payload = {"state": name} if name in {"OFFLINE", "ONLINE"} else {"subtype": name}
            kind = "collar_state" if "state" in payload else "event"
            await reader.process_line(serial_line({
                "schema": "vfence.gateway.serial.v1", "kind": kind, "collarId": 254, **payload,
            }))
    assert len(caplog.records) == 6
    assert {row["event_type"] for row in db.events()} == {
        "outside", "back_inside", "gps_lost", "gps_recovered", "collar_offline", "collar_online",
    }
    assert db.outbox_size() == 0
    for record in caplog.records:
        block = record.getMessage().splitlines()
        border = ALERT_RULE if record.levelno == logging.WARNING else BLOCK_RULE
        assert block[1] == block[-1] == border
        assert "[BASE][COL 254]" in block[2]
        assert "Origem: Gateway | Estado: EVENTO RECEBIDO" in block[3]
        assert "SQLite: COMMIT CONCLUIDO | Central: SINCRONIZACAO DESATIVADA" in block[4]


async def test_base_command_and_fence_logs_use_correlation_and_separate_stages(caplog):
    reader, _ = base_reader()
    loop = asyncio.get_running_loop()
    command = reader._pending_commands[4] = loop.create_future()
    fence = reader._fence_result = loop.create_future()
    reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 9, 2, 12
    schema = {"schema": "vfence.gateway.serial.v1"}
    transfer = {**schema, "kind": "fence_transfer", "requestId": 9, "collarId": 2, "fenceVersion": 12}
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        await reader.process_line(serial_line({
            **schema, "kind": "command_response", "command": "FENCE_SEND", "requestId": 4, "ok": True,
        }))
        await reader.process_line(serial_line({
            **schema, "kind": "command_response", "command": "FENCE_POINT", "requestId": 5, "ok": False,
            "detail": "point_invalid",
        }))
        await reader.process_line(serial_line({**transfer, "state": "started"}))
        await reader.process_line(serial_line({**transfer, "requestId": 8, "state": "confirmed"}))
        assert not fence.done()
        await reader.process_line(serial_line({**transfer, "state": "confirmed"}))
    assert command.result()["ok"] is True and fence.result() == (True, "collar_confirmed")
    assert "Resultado: ACEITO" in caplog.text and "Resultado: RECUSADO" in caplog.text
    assert "Estado: GATEWAY RESPONDEU" in caplog.text
    assert "GATEWAY REPORTOU INICIO DA TRANSFERENCIA" in caplog.text
    assert "RELATO DE CONFIRMACAO SEM CORRELACAO" in caplog.text
    assert caplog.text.count("GATEWAY REPORTOU ACK DA COLEIRA") == 1
    assert "ATIVADA" not in caplog.text


@pytest.mark.parametrize(("different", "value"), [
    ("requestId", 8), ("collarId", 3), ("fenceVersion", 11),
])
async def test_base_fence_failure_only_highlights_the_matching_operation(caplog, different, value):
    reader, db = base_reader()
    fence = reader._fence_result = asyncio.get_running_loop().create_future()
    reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 9, 2, 12
    current = {
        "schema": "vfence.gateway.serial.v1", "kind": "fence_transfer", "requestId": 9,
        "collarId": 2, "fenceVersion": 12, "state": "failed", "detail": "CRC_ERROR",
    }
    stale = {**current, different: value}
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        assert await reader.process_line(serial_line(stale))
        assert not fence.done()
        assert await reader.process_line(serial_line(current))
    ignored, failed = caplog.records
    assert ignored.levelno == logging.INFO
    assert ignored.getMessage().splitlines()[1] == BLOCK_RULE
    assert f"[COL {stale['collarId']:03d}] CERCA | RELATO SEM CORRELACAO" in ignored.getMessage()
    assert f"Request ID recebido: {stale['requestId']}" in ignored.getMessage()
    assert f"Versao reportada: {stale['fenceVersion']}" in ignored.getMessage()
    assert "Operacao atual: NAO ALTERADA" in ignored.getMessage()
    assert failed.levelno == logging.WARNING
    assert failed.getMessage().splitlines()[1] == ALERT_RULE
    assert "Request ID: 9" in failed.getMessage() and "CERCA | VERSAO 12" in failed.getMessage()
    assert fence.result() == (False, "CRC_ERROR")
    assert reader.hub.messages == [{"type": "fence_transfer", "data": packet} for packet in (stale, current)]
    assert db.fence_state()["confirmed_version"] is None
