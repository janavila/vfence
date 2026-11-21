import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import serial

from app.database import Database
from app.serial_reader import SerialReader
from test_metric_serial_export import MetricConnection, running, until
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
    assert caplog.text.count("TRANSICAO DE ZONA") == 1 and "SEGURO -> FORA" in caplog.text
    assert "Origem: telemetria persistida" in caplog.text and "[ZONA BASE] [COL 002] FORA -> SEGURO" in caplog.text
    assert "[ALERTA] [BASE / GATEWAY] [COL 002] OUTSIDE" in caplog.text
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
    assert "[POS] [BASE] [COL 001]" in logs
    assert "[APP]" not in logs
    assert "[SQLITE] INSERTED | COMMIT concluido" in logs
    assert "[COL 002] tipo=HDOP" in logs and "DUPLICATE" in logs and "CONFLICT" in logs
    assert "RADIO 60s | COL 254" in logs and "RSSI medio: -93.40 dBm | SNR medio: -3.75 dB" in logs
    assert "[ALERTA] [BASE / GATEWAY] [COL 007] GPS_LOST" in logs
    assert "[ACK FILA]" not in logs and "[ACK SERIAL]" not in logs
    assert len(db.telemetry("COL01")) == 1


async def test_base_reports_commit_queue_and_local_ack_in_order(tmp_path, caplog):
    db = Database(f"sqlite:///{tmp_path / 'terminal_ack.db'}")
    db.initialize()
    config = SimpleNamespace(
        mock_serial=False, serial_port="test", serial_baud=115200, reconnect_seconds=0,
        central_sync_enabled=False, metrics_serial_ack_enabled=True,
        metrics_installation_id="TEST", metrics_gateway_id="TEST",
    )
    reader = SerialReader(config, db, RecordingHub())
    port = MetricConnection(serial_line(hdop_json(collarId=7)))
    with caplog.at_level(logging.INFO, logger="serial_reader"):
        async with running(reader, port):
            await until(lambda: "escrita local aceita" in caplog.text)
    assert caplog.text.index("COMMIT concluido") < caplog.text.index("confirmacao enfileirada")
    assert caplog.text.index("confirmacao enfileirada") < caplog.text.index("escrita local aceita")
    assert "pendencia consumida" not in caplog.text
    assert len(port.writes) == 1 and b'"command":"METRIC_COMMITTED"' in port.writes[0]
