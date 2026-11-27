"""Launch the existing VFence Base and the JTAG transport from one Windows terminal."""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import getpass
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import warnings
from uuid import uuid4
from dataclasses import dataclass, replace
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from app.jtag_atlantic import quartus_directory


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / ".env.windows"
BASE_MUTEX_NAME = r"Global\VFence.Windows.Base"

DEFAULTS = {
    "SERIAL_PORT": "", "SERIAL_BAUD": "115200", "BASE_ID": "BASE01", "COLLAR_IDS": "",
    "DATABASE_URL": f"sqlite:///{(ROOT / 'vfence.db').as_posix()}",
    "VFENCE_HOST": "127.0.0.1", "VFENCE_PORT": "8000",
    "CENTRAL_SYNC_ENABLED": "false", "CENTRAL_BASE_URL": "http://127.0.0.1:8001", "BASE_TOKEN": "",
    "METRICS_INSTALLATION_ID": "", "METRICS_GATEWAY_ID": "", "METRICS_SERIAL_ACK_ENABLED": "false",
    "EVENT_SERIAL_COMMIT_ENABLED": "false", "PDR_SERIAL_COMMIT_ENABLED": "false",
    "PROFILE_CONTROL_ENABLED": "false", "PROFILE_CONTROL_TOKEN": "", "VFENCE_TERMINAL_DETAIL": "false",
    "VFENCE_MOCK_SERIAL": "false", "VFENCE_WINDOWS_BASE": "true", "VFENCE_FPGA_ENABLED": "true",
    "VFENCE_JTAG_DLL_DIR": r"C:\altera\13.1\quartus\bin64", "VFENCE_JTAG_CABLE": "USB-Blaster [USB-0]",
    "VFENCE_JTAG_DEVICE": "1", "VFENCE_JTAG_INSTANCE": "0",
    "VFENCE_HDOP_CLOCK_MAP": "{}",
    "VFENCE_GATEWAY_CLOCK_MAP": "{}",
    "VFENCE_FPGA_AUTO_PROFILE_ENABLED": "false",
    "VFENCE_GATEWAY_USB_ID": "", "VFENCE_GATEWAY_BOOT": "",
}


COM_PORT = re.compile(r"COM[1-9][0-9]*", re.I)
SESSION = re.compile(r"[0-9a-f]{16}")
SERIAL_SCHEMA = "vfence.gateway.serial.v1"
ACK_FLAGS = ("METRICS_SERIAL_ACK_ENABLED", "EVENT_SERIAL_COMMIT_ENABLED", "PDR_SERIAL_COMMIT_ENABLED")
BOOLEAN_SETTINGS = ("CENTRAL_SYNC_ENABLED", "VFENCE_FPGA_ENABLED", "PROFILE_CONTROL_ENABLED",
                    "VFENCE_TERMINAL_DETAIL", "VFENCE_FPGA_AUTO_PROFILE_ENABLED", *ACK_FLAGS)
ADVANCED = {
    "DATABASE_URL", "VFENCE_HOST", "VFENCE_PORT", "VFENCE_TERMINAL_DETAIL",
    "SERIAL_RECONNECT_SECONDS", "COLLAR_OFFLINE_SECONDS", "MAX_LINE_LENGTH", "MOCK_INTERVAL_SECONDS",
    "CENTRAL_TIMEOUT_SECONDS", "CENTRAL_SYNC_INTERVAL_SECONDS", "CENTRAL_RETRY_SECONDS", "CENTRAL_BATCH_SIZE",
    "COLLAR_TELEMETRY_SECONDS", "COLLAR_GPS_TIMEOUT_SECONDS", "COLLAR_LOW_BATTERY_PCT",
    "COLLAR_CRITICAL_BATTERY_PCT", "VFENCE_GATEWAY_CLOCK_MAP", "VFENCE_HDOP_CLOCK_MAP",
    "PROFILE_CONTROL_ENABLED", "VFENCE_FPGA_AUTO_PROFILE_ENABLED", "VFENCE_JTAG_DEVICE", "VFENCE_JTAG_INSTANCE", *ACK_FLAGS,
}
CENTRAL_KEYS = ("CENTRAL_SYNC_ENABLED", "CENTRAL_BASE_URL", "BASE_ID", "BASE_TOKEN", "CENTRAL_TIMEOUT_SECONDS",
                "CENTRAL_SYNC_INTERVAL_SECONDS", "CENTRAL_RETRY_SECONDS", "CENTRAL_BATCH_SIZE")
PROTECTED_TOKEN = "BASE_TOKEN_DPAPI"
FPGA_KEYS = tuple(key for key in DEFAULTS if key.startswith(("VFENCE_FPGA_", "VFENCE_JTAG_"))) + (
    "VFENCE_HDOP_CLOCK_MAP", "VFENCE_GATEWAY_CLOCK_MAP",
)
GATEWAY_KEYS = ("SERIAL_PORT", "SERIAL_BAUD", "METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID",
                "VFENCE_GATEWAY_USB_ID", "VFENCE_GATEWAY_BOOT", *ACK_FLAGS)


def enabled(values, key):
    return values.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def collar_ids(value):
    ids = []
    for item in re.split(r"[\s,;]+", value.strip()):
        if not item:
            continue
        match = re.fullmatch(r"(?:COL)?([0-9]{1,3})", item, re.I)
        if not match or not 1 <= int(match[1]) <= 254:
            raise ValueError("COLLAR_IDS deve conter IDs de 1 a 254.")
        collar = int(match[1])
        if collar in ids:
            raise ValueError("COLLAR_IDS contém IDs repetidos.")
        ids.append(collar)
    return ids


@dataclass
class GatewayProbe:
    connection: object
    backlog: bytearray
    detected: dict
    profile: dict
    usb_id: str = ""
    confirmed: bool = False
    disconnected: bool = False
    quarantined: bool = False

    @property
    def boot(self):
        value = self.profile.get("gatewaySessionId")
        return value if isinstance(value, str) and SESSION.fullmatch(value) else ""

    def close(self):
        if self.connection.is_open:
            with suppress(OSError):
                self.connection.close()


def quarantine_probe(probe):
    """Retain complete VFence records without allowing ingestion or an ACK."""
    if probe.quarantined:
        return
    records = []
    for line in bytes(probe.backlog).split(b"\n")[:-1]:
        if not line.startswith(b"[APP] "):
            continue
        try:
            data = json.loads(line[6:])
        except (ValueError, UnicodeError):
            continue
        if isinstance(data, dict) and data.get("schema") in (
            SERIAL_SCHEMA, "vfence.gateway.evidence.v1", "vfence.gateway.pdr.v1", "vfence.gateway.pdr.v2",
        ):
            records.append(line)
    if records:
        directory = SETTINGS_FILE.parent / ".vfence-unattributed"
        directory.mkdir(exist_ok=True)
        path = directory / f"{uuid4().hex}.capture"
        temporary = path.with_suffix(".tmp")
        with temporary.open("xb") as stream:
            stream.write(b"\n".join(records) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        print(f"Serial: {len(records)} quadro(s) isolados sem origem confirmada nem ACK.")
    probe.quarantined = True


def probe_gateway(port, baud):
    """Leave the same handle open so the backend can continue reading the stream."""
    import serial

    request_id = secrets.randbelow(0xFFFFFFFE) + 1
    connection = serial.Serial(port=None, baudrate=baud, timeout=0.1, write_timeout=0.3)
    probe = GatewayProbe(connection, bytearray(), {}, {})
    try:
        connection.dtr = connection.rts = False
        connection.port = port
        connection.open()
        pending = bytearray()

        def send(command, identifier):
            payload = dict(schema=SERIAL_SCHEMA, command=command, requestId=identifier)
            packet = b"\0\n[CMD] " + json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"
            if connection.write(packet) != len(packet):
                raise OSError("escrita incompleta no diagnóstico do Gateway")

        deadline = time.monotonic() + 3
        next_attempt = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not probe.confirmed and now >= next_attempt:
                send("GET_STATUS", request_id)
                next_attempt = now + 0.75
            chunk = connection.read(max(1, min(4096, connection.in_waiting)))
            probe.backlog.extend(chunk)
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                if not line.startswith(b"[APP] "):
                    continue
                try:
                    data = json.loads(line[6:])
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(data, dict) or data.get("schema") != SERIAL_SCHEMA:
                    continue
                kind = data.get("kind")
                if kind == "command_response" and data.get("ok") is True:
                    if type(data.get("requestId")) is not int:
                        continue
                    if not probe.confirmed and data.get("command") == "GET_STATUS" and data["requestId"] == request_id:
                        probe.confirmed = True
                        with suppress(OSError):
                            send("GET_PROFILE_V2", request_id + 1)
                        deadline = time.monotonic() + 0.65
                    elif probe.confirmed and data.get("command") == "GET_PROFILE_V2" and (
                        data.get("requestId") == request_id + 1 and data.get("profileControlVersion") == 2
                    ):
                        probe.profile = data
                if probe.confirmed and kind == "collar_status":
                    collar, state = data.get("collarId"), data.get("state")
                    if type(collar) is int and 1 <= collar <= 254 and state in ("ONLINE", "OFFLINE"):
                        probe.detected[collar] = state
            if len(pending) > 4096:
                pending.clear()
    except OSError:
        probe.disconnected = True
        probe.close()
        if not probe.backlog:
            raise
    except BaseException:
        probe.close()
        raise
    return probe


def gateway_usb_ids():
    from collections import Counter

    from serial.tools.list_ports import comports

    devices = {}
    for item in comports():
        if not COM_PORT.fullmatch(item.device):
            continue
        vendor, product = getattr(item, "vid", None), getattr(item, "pid", None)
        serial_number = getattr(item, "serial_number", None)
        devices[item.device.upper()] = (f"{vendor:04x}:{product:04x}:{serial_number}"
                                      if vendor is not None and product is not None and serial_number else "")
    counts = Counter(devices.values())
    return {port: identity if counts[identity] == 1 else "" for port, identity in devices.items()}


class GatewayDiscovery:
    def __init__(self):
        self.probes = {}

    def close(self, *, keep=None):
        for probe in self.probes.values():
            if probe is not keep:
                try:
                    quarantine_probe(probe)
                finally:
                    probe.close()

    def find(self, values, *, list_all=False):
        devices = gateway_usb_ids()
        saved = values["SERIAL_PORT"].upper()
        baud = int(values["SERIAL_BAUD"])
        bound_usb = values["VFENCE_GATEWAY_USB_ID"]

        def matches_binding(probe):
            return (list_all or not bound_usb or probe.usb_id == bound_usb
                    or (not probe.usb_id and probe.boot and probe.boot == values.get("VFENCE_GATEWAY_BOOT")))

        # Preserve the identity of a confirmed capture if the adapter was
        # unplugged or replaced while the menu was still open.
        detached = {port: probe for (port, rate), probe in self.probes.items()
                    if rate == baud and probe.disconnected
                    and matches_binding(probe) and (probe.confirmed or (probe.usb_id and probe.usb_id == bound_usb))}
        candidates = sorted(devices.keys() | detached.keys(), key=lambda port: (port != saved, int(port[3:])))
        matches = {}
        for port in candidates:
            if port in detached:
                matches[port] = detached[port]
                if port == saved and not list_all:
                    break
                continue
            key = port, baud
            identity = devices[port]
            if not list_all and bound_usb and identity and identity != bound_usb:
                continue
            probe = self.probes.get(key)
            if probe is not None and (not probe.connection.is_open or probe.usb_id != identity):
                quarantine_probe(probe)
                probe.close()
                del self.probes[key]
                probe = None
            if probe is None:
                for (old_port, old_baud), old_probe in self.probes.items():
                    if port == old_port and baud != old_baud:
                        quarantine_probe(old_probe)
                        old_probe.close()
                try:
                    probe = probe_gateway(port, baud)
                except OSError:
                    continue
                if identity and not probe.disconnected and gateway_usb_ids().get(port) != identity:
                    probe.close()
                    quarantine_probe(probe)
                    continue
                probe.usb_id = identity
                self.probes[key] = probe
            if ((probe.confirmed and (probe.connection.is_open or probe.disconnected))
                    or (probe.disconnected and probe.usb_id and probe.usb_id == bound_usb)):
                if not matches_binding(probe):
                    probe.close()
                    quarantine_probe(probe)
                    continue
                matches[port] = probe
                if port == saved and not list_all:
                    break
            elif probe.disconnected:
                quarantine_probe(probe)
        return candidates, matches


def show_collars(values, status):
    registered = collar_ids(values["COLLAR_IDS"])
    detected, profile = (status.detected, status.profile) if status else ({}, {})
    if not registered:
        print("COLLAR_IDS ausente. Cadastre as coleiras na opção 2.")

    def render(ids):
        return ", ".join(f"COL{collar:02d}" for collar in ids) or "—"

    participants = profile.get("participants")
    roster = [entry["collarId"] for entry in (participants if isinstance(participants, list) else [])
              if isinstance(entry, dict)
              and type(entry.get("collarId")) is int and 1 <= entry["collarId"] <= 254]
    print(f"Cadastradas (lista usada nas próximas transições): {render(registered)}")
    print(f"Detectadas pelo Gateway: {render(sorted(detected))} | "
          f"Online: {render(c for c, s in sorted(detected.items()) if s == 'ONLINE')} | "
          f"Offline: {render(c for c, s in sorted(detected.items()) if s == 'OFFLINE')}")
    print(f"Participantes informadas pelo Gateway: {render(roster)}")


def edit(values, key, label, *, show_value=True):
    prompt = f"{label} [{values[key]}]" if show_value else f"{label} (Enter mantém)"
    value = input(f"{prompt}: ").strip()
    if value:
        values[key] = value


def read_token(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning:
            print("Abra um PowerShell interativo para digitar a credencial com entrada oculta.")
            return ""


def windows_token(data: bytes, *, protect: bool) -> bytes:
    """Protect the credential for this Windows user without DPAPI prompts."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), source_buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = (ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob))
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes, free.restype = (ctypes.c_void_p,), ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(result.data)


def configure_gateway(values, discovery):
    edit(values, "SERIAL_BAUD", "Baud")
    positive_integer(values, "SERIAL_BAUD")
    ports, matches = discovery.find(values, list_all=True)
    print("COM disponíveis: " + (", ".join(ports) or "nenhuma"))
    print("Gateways confirmados: " + (", ".join(matches) or "nenhum"))
    default = values["SERIAL_PORT"].upper()
    if len(matches) == 1:
        default = next(iter(matches))
    selected = input("COM para o Gateway (Enter mantém/sugere a seleção): ").strip().upper() or default
    if selected and not COM_PORT.fullmatch(selected):
        raise ValueError("Informe uma porta como COM5.")
    values["SERIAL_PORT"] = selected
    edit(values, "METRICS_GATEWAY_ID", "ID estável do Gateway para RADIO/PDR")
    edit(values, "METRICS_INSTALLATION_ID", "ID estável da instalação para HDOP")
    if values["METRICS_GATEWAY_ID"] or values["METRICS_INSTALLATION_ID"]:
        current = "s" if enabled(values, "METRICS_SERIAL_ACK_ENABLED") else "n"
        ack = input(f"Gateway com exportação de médias e METRIC_COMMITTED confirmados? [s/n, Enter={current}]: ")
        if ack.strip().lower() in ("s", "n"):
            values["METRICS_SERIAL_ACK_ENABLED"] = str(ack.strip().lower() == "s").lower()
    return matches.get(selected)


def positive_integer(values, key, lo=1, hi=None):
    try:
        result = int(values[key])
        if result >= lo and (hi is None or result <= hi):
            return result
    except (KeyError, ValueError):
        pass
    raise ValueError(f"Valor inválido para {key}.")


def validate_base(values):
    for key in BOOLEAN_SETTINGS:
        if values[key].strip().lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"Valor inválido para {key}. Use true ou false.")
    for key, lo, hi in (("SERIAL_BAUD", 1, None), ("VFENCE_PORT", 1, 65535), ("MAX_LINE_LENGTH", 1, None),
                        ("COLLAR_OFFLINE_SECONDS", 1, None), ("COLLAR_TELEMETRY_SECONDS", 1, 60),
                        ("COLLAR_GPS_TIMEOUT_SECONDS", 1, None), ("COLLAR_LOW_BATTERY_PCT", 0, 100),
                        ("COLLAR_CRITICAL_BATTERY_PCT", 0, 100), ("CENTRAL_BATCH_SIZE", 1, 500)):
        if key in values:
            positive_integer(values, key, lo, hi)
    for key in ("SERIAL_RECONNECT_SECONDS", "MOCK_INTERVAL_SECONDS", "CENTRAL_TIMEOUT_SECONDS",
                "CENTRAL_SYNC_INTERVAL_SECONDS", "CENTRAL_RETRY_SECONDS"):
        if key in values:
            try:
                number = float(values[key])
                if math.isfinite(number) and number > 0:
                    continue
            except ValueError:
                pass
            raise ValueError(f"Valor inválido para {key}.")
    db_url = values["DATABASE_URL"]
    file_path = db_url.removeprefix("sqlite:///")
    if file_path == db_url or not file_path.strip() or file_path == ":memory:":
        raise ValueError("Informe DATABASE_URL como sqlite:///C:/caminho/arquivo.db em Avançadas.")
    database = Path(file_path).resolve()
    if not database.parent.is_dir() or database.is_dir():
        raise ValueError("O caminho de DATABASE_URL deve apontar para um arquivo em um diretório existente.")
    values["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    ids = collar_ids(values["COLLAR_IDS"])
    values["COLLAR_IDS"] = ",".join(f"COL{collar:02d}" for collar in ids)


def usb_blaster_present():
    """Query Windows PnP without taking ownership of JTAG."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-PnpDevice -PresentOnly -ErrorAction Stop | "
             "Where-Object FriendlyName -Match 'USB[ -]Blaster') { 'VFENCE_BLASTER' }"],
            capture_output=True, timeout=4, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return b"VFENCE_BLASTER" in result.stdout if result.returncode == 0 else None


def validate_fpga(values, runtime):
    invalid_index = False
    for key in ("VFENCE_JTAG_DEVICE", "VFENCE_JTAG_INSTANCE"):
        try:
            positive_integer(values, key, 1 if key.endswith("DEVICE") else 0)
        except ValueError as error:
            runtime[key] = DEFAULTS[key]  # Settings parses these even with FPGA disabled.
            runtime["VFENCE_FPGA_ENABLED"] = "false"
            print(f"FPGA indisponível: {error} Confira Avançadas.")
            invalid_index = True
    if invalid_index:
        return False
    from app.fpga_service import GatewayClock

    try:
        gateway_map = json.loads(values["VFENCE_GATEWAY_CLOCK_MAP"])
        hdop_map = json.loads(values["VFENCE_HDOP_CLOCK_MAP"])
        for key, reference in gateway_map.items():
            if not SESSION.fullmatch(key):
                raise ValueError
            GatewayClock.from_settings(reference)
        for key, offset in hdop_map.items():
            if not re.fullmatch(r"[0-9a-f]{16}/[0-9a-f]{16}", key) or type(offset) is not int:
                raise ValueError
    except (ValueError, AttributeError, OverflowError, KeyError, TypeError):
        runtime["VFENCE_FPGA_ENABLED"] = "false"
        print("FPGA indisponível: confira VFENCE_GATEWAY_CLOCK_MAP e VFENCE_HDOP_CLOCK_MAP em Avançadas.")
        return False
    if not enabled(runtime, "VFENCE_FPGA_ENABLED"):
        return True
    from app.fpga_window import MAX_GROUP_SIZE

    if not 1 <= len(collar_ids(values["COLLAR_IDS"])) <= MAX_GROUP_SIZE:
        runtime["VFENCE_FPGA_ENABLED"] = "false"
        print(f"FPGA indisponível: configure de 1 a {MAX_GROUP_SIZE} coleiras para votação. Os cadastros foram preservados.")
        return False
    directory = quartus_directory(values["VFENCE_JTAG_DLL_DIR"])
    if directory is None:
        print("FPGA: aguardando as DLLs do Quartus bin64. A Base continuará tentando.")
        return False
    if not values["VFENCE_JTAG_CABLE"].strip():
        runtime["VFENCE_FPGA_ENABLED"] = "false"
        print("FPGA indisponível: indique o cabo em Configurar FPGA.")
        return False
    values["VFENCE_JTAG_DLL_DIR"] = runtime["VFENCE_JTAG_DLL_DIR"] = str(directory)
    cable = usb_blaster_present()
    from app.terminal import terminal_block

    print(terminal_block("FPGA", "INICIALIZACAO", "Quartus encontrado.",
                         "USB-Blaster detectado." if cable else "USB-Blaster nao confirmado pelo Windows.",
                         "Estado: AGUARDANDO ABERTURA JTAG E PONG", banner=True))
    if not all(runtime[key].strip() for key in ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID")):
        print("FPGA em diagnóstico: confirme os IDs de métricas para votação real.")
    return cable is not False


def check_central(values):
    import httpx

    url = values["CENTRAL_BASE_URL"]
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.query or parts.fragment or not values["BASE_ID"].strip()
                or len(values["BASE_ID"]) > 64):
            return "configuração inválida"
        _ = parts.port
    except ValueError:
        return "configuração inválida"
    token = values["BASE_TOKEN"].strip()
    if not token:
        return "token ausente"
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        return "HTTP 401"
    try:
        # Empty batch authenticates BASE_ID without reporting a fictitious heartbeat or sending records.
        response = httpx.post(url.rstrip("/") + "/api/edge/batch", timeout=3,
                              headers={"Authorization": f"Bearer {token}"},
                              json={"base_id": values["BASE_ID"], "items": []})
        if response.status_code in (401, 403):
            return f"HTTP {response.status_code}"
        if response.status_code == 200 and type(response.json().get("acked_up_to")) is int:
            return "autenticada"
        if response.status_code not in (408, 425, 429) and response.status_code < 500:
            return "resposta ou endereço da Central incompatível"
    except (ValueError, AttributeError):
        return "resposta ou endereço da Central incompatível"
    except httpx.HTTPError:
        return "indisponível"
    return "indisponível"


def prepare_central(values, interactive):
    if not enabled(values, "CENTRAL_SYNC_ENABLED"):
        return "desativada"
    status = check_central(values)
    if interactive and status in {"token ausente", "HTTP 401"}:
        entered = read_token("Central: informe o BASE_TOKEN provisionado (Enter para operar localmente): ")
        if entered:
            candidate = {**values, "BASE_TOKEN": entered}
            status = check_central(candidate)
            if status == "autenticada":
                values["BASE_TOKEN"] = entered
            elif status == "indisponível":
                print("Central indisponível para validar a nova credencial. A configuração anterior foi guardada.")
                return "credencial nova ainda não validada"
    if status == "autenticada":
        values["BASE_TOKEN"] = values["BASE_TOKEN"].strip()
        print("Central autenticada.")
        return status
    if status == "indisponível":
        print("Central indisponível. A Base local continuará gravando e a sincronização tentará novamente.")
        return status
    print(f"Central em modo local ({status}). Confira BASE_ID, URL e a credencial provisionada em Configurar Central.")
    return status


def prepare_acks(values, runtime, gateway_verified):
    for key in ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID"):
        value = values[key].strip()
        if not value or "\0" in value:
            runtime[key] = ""
            purpose = "RADIO/PDR" if key == "METRICS_GATEWAY_ID" else "HDOP"
            print(f"{purpose}: configure {key} em Configurar Gateway para persistir os resumos.")
        else:
            values[key] = runtime[key] = value
    if not (runtime["METRICS_GATEWAY_ID"] or runtime["METRICS_INSTALLATION_ID"]):
        runtime["METRICS_SERIAL_ACK_ENABLED"] = "false"
    if not runtime["METRICS_GATEWAY_ID"]:
        runtime["PDR_SERIAL_COMMIT_ENABLED"] = "false"
    if enabled(runtime, "PROFILE_CONTROL_ENABLED") and not runtime["PROFILE_CONTROL_TOKEN"].strip():
        runtime["PROFILE_CONTROL_ENABLED"] = "false"
        print("Controle manual de perfil desativado nesta execução: configure o token em Avançadas.")
    if not enabled(runtime, "METRICS_SERIAL_ACK_ENABLED"):
        print("ACK de médias desativado. Use Configurar Gateway se o firmware exporta HDOP/RADIO "
              "e aceita METRIC_COMMITTED.")
    if any(enabled(runtime, key) for key in ACK_FLAGS):
        print("ACKs solicitados: " + ", ".join(key for key in ACK_FLAGS if enabled(runtime, key)) + ". "
              "Eventos e PDR negociam suporte no Gateway." if gateway_verified else
              "ACKs mantidos conforme configuração. Suporte do Gateway ainda não verificado.")


def select_gateway(values, discovery):
    _, matches = discovery.find(values)
    saved = values["SERIAL_PORT"].upper()
    if saved not in matches:
        if len(matches) == 1:
            saved = next(iter(matches))
        elif len(matches) > 1:
            raise ValueError("Mais de um Gateway respondeu. Selecione a COM em Configurar Gateway.")
    values["SERIAL_PORT"] = saved
    status = matches.get(saved)
    if status and status.disconnected:
        print(f"Gateway: {saved} | desconectado durante a sondagem. A Base recuperará os quadros completos.")
    else:
        print(f"Gateway: {saved} | " + ("GET_STATUS confirmado." if status else
              "sem resposta na verificação. A Base tentará reconectar."))
    show_collars(values, status)
    return status


def settings_menu(values, discovery):
    gateway_status = None
    selected_gateway = None
    while True:
        print("\n1. Iniciar VFence [Enter]\n2. Configurar coleiras\n3. Configurar Central\n"
              "4. Configurar Gateway\n5. Configurar FPGA\n6. Configurações avançadas\n7. Sair")
        choice = input("Opção: ").strip() or "1"
        try:
            if choice == "7":
                return False, None
            if choice == "1":
                validate_base(values)
                return True, selected_gateway or select_gateway(values, discovery)
            if choice == "2":
                if gateway_status is None:
                    positive_integer(values, "SERIAL_BAUD")
                    _, matches = discovery.find(values)
                    gateway_status = matches.get(values["SERIAL_PORT"].upper())
                    if gateway_status is None and len(matches) == 1:
                        gateway_status = next(iter(matches.values()))
                show_collars(values, gateway_status)
                entered = input("IDs cadastrados (ex. 1, 2, 10). Enter mantém: ").strip()
                if entered:
                    values["COLLAR_IDS"] = ",".join(f"COL{collar:02d}" for collar in collar_ids(entered))
            elif choice == "3":
                edit(values, "CENTRAL_SYNC_ENABLED", "Sincronização Central (true/false)")
                edit(values, "CENTRAL_BASE_URL", "Endereço da Central", show_value=False)
                edit(values, "BASE_ID", "Identidade da Base")
            elif choice == "4":
                selected_gateway = gateway_status = configure_gateway(values, discovery)
            elif choice == "5":
                edit(values, "VFENCE_FPGA_ENABLED", "FPGA (true/false)")
                edit(values, "VFENCE_JTAG_DLL_DIR", "Diretório bin64 do Quartus")
                edit(values, "VFENCE_JTAG_CABLE", "Cabo USB-Blaster")
            elif choice == "6":
                print("Ajustes disponíveis: " + ", ".join(sorted(ADVANCED)))
                print("EVENT_SERIAL_COMMIT_ENABLED e PDR_SERIAL_COMMIT_ENABLED exigem suporte no firmware. "
                      "PDR v2 também exige exportação PDR habilitada.")
                key, sep, value = input("Chave=valor (ou PROFILE_CONTROL_TOKEN para entrada oculta): ").partition("=")
                key = key.strip().upper()
                if key == "PROFILE_CONTROL_TOKEN" and not sep and not values[key].strip():
                    values[key] = read_token("Token de controle de perfil: ")
                elif sep and key in ADVANCED and value.strip():
                    values[key] = value.strip()
                elif key:
                    print("Ajuste não alterado.")
            else:
                print("Escolha uma opção de 1 a 7.")
        except ValueError as error:
            print(error)


def restore_group(values, previous, keys):
    for key in keys:
        if key in previous:
            values[key] = previous[key]
        else:
            values.pop(key, None)


def group_changed(values, previous, keys):
    return any(values.get(key) != previous.get(key) for key in keys)


def prepare_gateway(values, runtime, previous, probe, *, interactive):
    old_id = previous["METRICS_GATEWAY_ID"].strip()
    requested = values["METRICS_GATEWAY_ID"].strip()
    bound_usb = previous["VFENCE_GATEWAY_USB_ID"]
    bound_boot = previous["VFENCE_GATEWAY_BOOT"]
    stable_match = bool(probe and probe.usb_id and probe.usb_id == bound_usb)
    known_replacement = bool(probe and probe.usb_id and bound_usb and probe.usb_id != bound_usb)
    same_boot = bool(probe and probe.boot and probe.boot == bound_boot and not known_replacement)
    verified = bool(probe and ((probe.confirmed and (probe.connection.is_open or probe.disconnected))
                               or (stable_match and probe.disconnected)))
    if not interactive and bound_usb and probe is None:
        # The reconnect factory will enforce the binding before any read or ACK.
        return False

    if verified and (old_id or requested) and not (stable_match or same_boot):
        if interactive:
            if old_id and not known_replacement:
                same = input("É o mesmo Gateway físico previamente configurado? [s/N]: ").strip().lower() == "s"
            else:
                same = False
            if same:
                requested = old_id
            elif requested and requested != old_id:
                same = input("Confirmar o ID informado para este Gateway físico? [s/N]: ").strip().lower() == "s"
                if not same:
                    requested = ""
            else:
                requested = input("ID provisionado do novo Gateway (Enter para operar sem métricas): ").strip()
                if requested == old_id:
                    requested = ""
        else:
            requested = ""

    if (not verified or "\0" in requested or "\0" in values["METRICS_INSTALLATION_ID"]
            or (bool(old_id or values["METRICS_GATEWAY_ID"].strip()) and not requested)):
        restore_group(values, previous, GATEWAY_KEYS)
        if not verified and COM_PORT.fullmatch(previous["SERIAL_PORT"]):
            runtime["SERIAL_PORT"] = previous["SERIAL_PORT"].upper()
            runtime["SERIAL_BAUD"] = previous["SERIAL_BAUD"]
        for key in ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID"):
            runtime[key] = ""
        for key in ACK_FLAGS:
            runtime[key] = "false"
        print("Identidade do Gateway não confirmada. Configuração salva preservada. "
              "Métricas e confirmações desativadas para esta execução.")
        return False

    values["METRICS_GATEWAY_ID"] = runtime["METRICS_GATEWAY_ID"] = requested
    values["VFENCE_GATEWAY_USB_ID"] = runtime["VFENCE_GATEWAY_USB_ID"] = probe.usb_id or (bound_usb if same_boot else "")
    if probe.boot:
        values["VFENCE_GATEWAY_BOOT"] = runtime["VFENCE_GATEWAY_BOOT"] = probe.boot
    return True


def provision_local_metrics(values):
    # A COM number or a boot session cannot establish a persistent identity.
    if not values["VFENCE_GATEWAY_USB_ID"]:
        return
    generated = False
    for key, prefix in (("METRICS_GATEWAY_ID", "local-gw"), ("METRICS_INSTALLATION_ID", "local-site")):
        if not values.get(key, "").strip():
            values[key] = f"{prefix}-{uuid4().hex}"
            generated = True
    if generated:
        values["METRICS_SERIAL_ACK_ENABLED"] = "true"
    return generated


def save_settings(values):
    if values.get(PROTECTED_TOKEN):
        values = {key: value for key, value in values.items() if key != "BASE_TOKEN"}
    temporary = SETTINGS_FILE.with_suffix(".windows.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(SETTINGS_FILE)


def load_settings(reuse: bool, discovery):
    from dotenv import dotenv_values

    values = DEFAULTS.copy()
    for path in (ROOT / ".env", SETTINGS_FILE):
        values.update({key: value for key, value in dotenv_values(path, interpolate=False).items()
                       if value is not None})
    if values.get(PROTECTED_TOKEN):
        try:
            encrypted = base64.b64decode(values[PROTECTED_TOKEN], validate=True)
            values["BASE_TOKEN"] = windows_token(encrypted, protect=False).decode("ascii")
        except (OSError, ValueError):
            values["BASE_TOKEN"] = ""
            print("Central: credencial protegida indisponível nesta conta. Use --configure.")
    values.update(VFENCE_WINDOWS_BASE="true", VFENCE_MOCK_SERIAL="false")
    previous = values.copy()
    if reuse:
        validate_base(values)
        try:
            probe = select_gateway(values, discovery)
        except ValueError as error:
            print(f"Gateway em espera. {error}")
            probe = None
    else:
        start, probe = settings_menu(values, discovery)
        if not start:
            return None
    central_status = ("aguardando sincronização" if enabled(values, "CENTRAL_SYNC_ENABLED") else "desativada")
    if not reuse:
        central_status = prepare_central(values, interactive=True)
    if central_status not in {"autenticada", "desativada"} and group_changed(values, previous, CENTRAL_KEYS):
        restore_group(values, previous, CENTRAL_KEYS)
        print("A configuração anterior da Central foi restaurada.")
        central_status = prepare_central(values, interactive=False)
    if central_status == "autenticada" and values["BASE_TOKEN"] != previous["BASE_TOKEN"]:
        try:
            protected = windows_token(values["BASE_TOKEN"].encode("ascii"), protect=True)
        except OSError:
            restore_group(values, previous, CENTRAL_KEYS)
            print("Central: falha ao proteger a credencial. A configuração anterior foi preservada.")
            central_status = "credencial nova não armazenada"
        else:
            values[PROTECTED_TOKEN] = base64.b64encode(protected).decode("ascii")
    central_error = None if central_status in {"autenticada", "desativada", "indisponível", "aguardando sincronização"} else central_status
    runtime = values.copy()
    runtime.pop(PROTECTED_TOKEN, None)
    accepted = prepare_gateway(values, runtime, previous, probe, interactive=not reuse)
    if probe is not None and not accepted:
        probe.close()
        quarantine_probe(probe)
        probe = None
    if (reuse and runtime["VFENCE_GATEWAY_USB_ID"]
            and (probe is None or probe.usb_id == runtime["VFENCE_GATEWAY_USB_ID"])
            and provision_local_metrics(values)):
        restore_group(runtime, values, ("METRICS_GATEWAY_ID", "METRICS_INSTALLATION_ID", "METRICS_SERIAL_ACK_ENABLED"))
    prepare_acks(runtime, runtime, bool(probe))
    if not validate_fpga(values, runtime) and group_changed(values, previous, FPGA_KEYS):
        restore_group(values, previous, FPGA_KEYS)
        restore_group(runtime, previous, FPGA_KEYS)
        print("A configuração anterior da FPGA foi restaurada.")
        validate_fpga(values, runtime)
    if not reuse or (values != previous and (probe is not None or previous["VFENCE_GATEWAY_USB_ID"])):
        save_settings(values)
    return runtime, central_error, probe


def checked_reopen(port, baud, initial, *, usb_id=None, boot=None):
    usb_id = initial.usb_id if usb_id is None and initial else usb_id
    boot = initial.boot if boot is None and initial else boot
    devices = gateway_usb_ids()
    if usb_id:
        port = next((name for name, identity in devices.items() if identity == usb_id), port)
    identity = devices.get(port, "")
    if identity and usb_id and identity != usb_id:
        raise ConnectionError("Outro Gateway detectado. Reinicie pelo menu para confirmar a identidade.")
    if not port or (usb_id and identity != usb_id and not boot):
        raise ConnectionError("Aguardando o Gateway configurado")
    probe = probe_gateway(port, baud)
    if identity and not probe.disconnected and gateway_usb_ids().get(port) != identity:
        probe.close()
        quarantine_probe(probe)
        raise ConnectionError("Identidade USB mudou durante a verificação do Gateway")
    if ((probe.confirmed and ((identity and identity == usb_id) or (probe.boot and probe.boot == boot)))
            or (probe.disconnected and identity and identity == usb_id)):
        return (None if probe.disconnected else probe.connection), bytes(probe.backlog)
    probe.close()
    quarantine_probe(probe)
    raise ConnectionError("Identidade do Gateway não verificada. Reinicie pelo menu se o equipamento mudou.")


def enroll_late_gateway(probe):
    """Bind a first Gateway with stable USB identity before handing off frames."""
    from dotenv import dotenv_values

    if not probe.confirmed or not probe.usb_id:
        return None
    saved = {k: v for k, v in dotenv_values(SETTINGS_FILE, interpolate=False).items() if v is not None}
    inherited = dotenv_values(ROOT / ".env", interpolate=False)
    configured = {**inherited, **saved}
    if configured.get("VFENCE_GATEWAY_USB_ID") or configured.get("VFENCE_GATEWAY_BOOT") or configured.get("METRICS_GATEWAY_ID"):
        raise ConnectionError("Existe uma identidade anterior sem vínculo comprovado com este Gateway")
    saved.update(VFENCE_GATEWAY_USB_ID=probe.usb_id, VFENCE_GATEWAY_BOOT=probe.boot,
                 SERIAL_PORT=probe.connection.port,
                 METRICS_INSTALLATION_ID=configured.get("METRICS_INSTALLATION_ID") or "")
    provision_local_metrics(saved)
    save_settings(saved)
    return {
        "serial_port": probe.connection.port,
        "metrics_gateway_id": saved["METRICS_GATEWAY_ID"],
        "metrics_installation_id": saved["METRICS_INSTALLATION_ID"],
        "metrics_serial_ack_enabled": True,
    }


async def launch(central_error=None, probe=None):
    import uvicorn

    from app import main as base

    app, reader, settings, sync_service = base.app, base.reader, base.settings, base.sync_service

    if probe is not None:
        reader.adopt_windows_serial(probe.connection, bytes(probe.backlog), detached=probe.disconnected)
    usb_id = os.getenv("VFENCE_GATEWAY_USB_ID", "")
    boot = os.getenv("VFENCE_GATEWAY_BOOT", "")
    provisioned = None

    def reopen():
        nonlocal usb_id, boot, provisioned
        if not usb_id and not boot:
            discovery = GatewayDiscovery()
            try:
                _, matches = discovery.find({"SERIAL_PORT": reader.serial_port, "SERIAL_BAUD": str(settings.serial_baud),
                                             "VFENCE_GATEWAY_USB_ID": ""}, list_all=True)
                if len(matches) != 1:
                    raise ConnectionError("Aguardando um Gateway identificável")
                selected = next(iter(matches.values()))
                discovery.close(keep=selected)
                provisioned = enroll_late_gateway(selected)
                usb_id, boot = selected.usb_id, selected.boot
                selected.quarantined = True  # This buffer is owned by the backend.
                return (None if selected.disconnected else selected.connection), bytes(selected.backlog)
            except BaseException:
                discovery.close()
                raise
        return checked_reopen(reader.serial_port, settings.serial_baud, probe, usb_id=usb_id, boot=boot)

    def apply_binding():
        nonlocal provisioned
        if provisioned is None:
            return
        updated = replace(base.settings, **provisioned)
        base.settings = reader.config = base.fpga_service.config = sync_service.config = updated
        provisioned = None
        print("Gateway identificado. Identidades locais salvas e ACKs de métricas habilitados após COMMIT.")

    # Never open a saved COM unguarded after unplug/replug.
    reader.set_windows_serial_reopen(reopen, reopened=apply_binding)

    if central_error:
        # The worker retries authentication at its existing bounded interval.
        sync_service.last_error = central_error

    loop = asyncio.get_running_loop()

    async def serial_command(command):
        if command == "liberar":
            await reader.release_serial()
            print("COM liberada. Abra o Monitor Serial da Arduino IDE.", flush=True)
        else:
            reader.resume_serial()
            print("Reconexão solicitada. Acompanhe o log do Gateway.", flush=True)

    def console():
        print("Comandos neste terminal: liberar | reconectar. Ctrl+C encerra a Base.", flush=True)
        for line in sys.stdin:
            command = line.strip().lower()
            if command not in {"liberar", "reconectar"}:
                if command:
                    print("Use liberar ou reconectar.", flush=True)
                continue
            try:
                asyncio.run_coroutine_threadsafe(serial_command(command), loop).result(timeout=12)
            except Exception:
                print("Operação COM sem confirmação. Confira o log antes de abrir o Monitor Serial.", flush=True)

    if sys.stdin.isatty():
        threading.Thread(target=console, daemon=True, name="vfence-console").start()
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, workers=1,
                                          access_log=settings.terminal_detail))
    await server.serve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reuse", action="store_true", help="reutiliza .env.windows sem perguntas (modo padrão)")
    mode.add_argument("--configure", action="store_true", help="abre o menu de configuração")
    parser.add_argument("--detail", action="store_true", help="ativa o diagnóstico detalhado nesta execução")
    args = parser.parse_args()
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        parser.exit(1, "Execute com Python Windows de 64 bits.\n")
    os.chdir(ROOT)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, BASE_MUTEX_NAME)
    if not handle:
        parser.exit(1, "Falha ao reservar a instância da Base.\n")
    try:
        if ctypes.get_last_error() == 183:
            parser.exit(1, "Já existe uma Base Windows em execução.\n")
        discovery = GatewayDiscovery()
        configuration = load_settings(not args.configure, discovery)
        if configuration is None:
            return
        values, central_error, probe = configuration
        if args.detail:
            values["VFENCE_TERMINAL_DETAIL"] = "true"
        if any(other.confirmed for other in discovery.probes.values() if other is not probe):
            print("Mensagens das outras COM não serão vinculadas ao Gateway selecionado.")
        discovery.close(keep=probe)
        if probe is not None:
            probe.quarantined = True  # This buffer is owned by the backend.
        os.environ.update(values)
        asyncio.run(launch(central_error, probe))
    except (EOFError, KeyboardInterrupt):
        return
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"Falha ao iniciar VFence: {type(error).__name__}. Confira configuração e dependências.\n")
    finally:
        if "discovery" in locals():
            discovery.close()
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
