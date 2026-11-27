"""Human-readable diagnostics shared by the Base and the PC Serial client."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import OrderedDict
from datetime import datetime
from itertools import groupby

from app.fpga_window import FpgaSample
from app.metric_evidence import EVIDENCE_KIND, EVIDENCE_SCHEMA, MetricEvidence
from app.metric_protocol import MetricSummary
from app.pdr_protocol import PdrSummary


RULE = "=" * 56
ALERT_RULE = "!" * 56
BLOCK_RULE = "-" * 56
ALERT_EVENTS = {"OUTSIDE", "GPS_LOST", "LOW_BATTERY", "CRITICAL_BATTERY", "OFFLINE"}
_CONTROL_CHARS = dict.fromkeys([
    *range(32), *range(127, 160), 0x061C, *range(0x200B, 0x2010),
    *range(0x2028, 0x202F), *range(0x2060, 0x2070), 0xFEFF,
], " ")
_GATEWAY_LOG_ENUMS = {
    "schema": {"vfence.gateway.serial.v1", "vfence.gateway.pdr.v1", "vfence.gateway.pdr.v2"},
    "kind": {
        "telemetry", "event", "control", "collar_state", "collar_status", "offline",
        "gateway_state", "radio_config", "command_response", "fence_transfer",
        "hdop_window_summary", "radio_window_summary", "pdr_window_summary",
    },
    "state": {"ONLINE", "OFFLINE", "online", "offline", "started", "confirmed", "failed", "IDLE", "PLANNED"},
    "subtype": ALERT_EVENTS | {"BACK_INSIDE", "GPS_RECOVERED", "DEVICE_STARTED", "ACK", "NACK", "PONG"},
    "zone": {"SEGURO", "ATENCAO", "CRITICO", "FORA", "GNSS_INVALIDO"},
    "command": {
        "GET_STATUS", "GET_RADIO", "GET_PROFILE", "LIST_PROFILES", "GET_PROFILE_TRANSITION",
        "REQUEST_PROFILE", "CANCEL_PROFILE", "PING", "SET_PARAMETERS", "FENCE_NEW", "FENCE_POINT",
        "FENCE_SEND", "FENCE_CANCEL", "ENABLE_EVENT_COMMIT", "ENABLE_PDR_COMMIT", "UNKNOWN",
        "GET_PROFILE_V2", "START_PROFILE_V2", "CANCEL_PROFILE_V2", "GET_METRIC_CLOCK_V1",
        "METRIC_COMMITTED", "EVIDENCE_COMMITTED", "EVENT_COMMITTED", "PDR_COMMITTED",
    },
    "reason": {"ok", "no_samples", "one_sample", "session_changed", "profile_changed", "discontinuity"},
    "detail": {
        "ACK_TIMEOUT", "INVALID_LENGTH", "UNKNOWN_SUBTYPE", "INVALID_COLLAR_ID", "UNSUPPORTED_VERSION",
        "INVALID_PARAMETER", "INVALID_FENCE_VERSION", "INVALID_CHUNK", "MISSING_CHUNKS", "CRC_ERROR",
        "COMMIT_REJECTED", "BUSY", "PERSISTENCE_ERROR", "TX_FAILED", "invalid_json", "request_id_invalid",
        "command_invalid", "schema_invalid", "collar_id_invalid", "token_invalid", "parameters_invalid",
        "vertex_count_invalid", "point_invalid", "point_index_invalid", "fence_not_staged", "command_unknown",
        "radio_send_failed", "CANCELLED", "gateway_confirmation_timeout", "serial_disconnected",
        "collar_confirmed", "set_parameters_rejected", "fence_new_rejected", "fence_point_rejected",
        "fence_send_rejected", "profile_status", "plan_registered", "plan_cancelled", "duplicate_operation",
        "already_active", "invalid_profile_operation", "gateway_session_mismatch", "stale_operation", "busy",
    },
}
_GATEWAY_LOG_NUMBERS = {
    "requestId", "collarId", "sequence", "fenceVersion", "latitude", "longitude", "distanceBorderM",
    "satellites", "hdop", "rssi", "snr", "battery", "durationMs", "sampleCount",
    "collarStartedAtMs", "gatewayStartedAtMs", "gatewayReceivedAtMs", "startedAtMs",
    "hdopMeanX100", "rssiMeanX100", "snrMeanX100", "pdrX100", "profileId", "activeProfileId",
    "requestedProfileId", "profileControlVersion", "receivedCount", "estimatedTxCount", "estimatedLostCount",
    "frequencyHz", "spreadingFactor", "bandwidthHz", "codingRateDenominator", "preambleLength", "txPowerDbm",
    "syncWord", "vertexCount", "ackedSequence", "errorCode", "lastSeenS", "operationId",
}
_GATEWAY_LOG_FLAGS = {
    "ok", "gpsValid", "inside", "near", "veryNear", "hdopMeanValid", "rssiMeanValid", "snrMeanValid",
    "valid", "empty", "crcEnabled", "fenceTransferActive", "switchEnabled",
}
_SQLITE_RESULTS = {
    "INSERTED": "INSERTED | COMMIT CONCLUIDO",
    "DUPLICATE": "DUPLICATE | REGISTRO IDENTICO JA PERSISTIDO",
    "CONFLICT": "CONFLICT | IDENTIDADE COM CONTEUDO DIVERGENTE",
    "FAILED": "FAILED | FALHA DE PERSISTENCIA",
}


def collar_label(value: int | str | None) -> str:
    match = re.fullmatch(r"(?:COL\s*)?([0-9]{1,3})", str(value), re.IGNORECASE)
    if match and 1 <= int(match[1]) <= 254:
        return f"COL {int(match[1]):03d}"
    return "COL N/A"


def value_text(value: object) -> str:
    return "N/A" if value is None else str(value).replace("\r", " ").replace("\n", " ")


def _number(value: int | float | None, format_spec: str, unit: str = "") -> str:
    if type(value) not in (int, float):
        return "N/A"
    try:
        return f"{value:{format_spec}}{unit}" if math.isfinite(value) else "N/A"
    except OverflowError:
        return "N/A"


def position_line(origin: str, collar_id: int | str | None, sequence: int | None,
                  gps_valid: bool | None, hdop: float | None, rssi: float | None,
                  snr: float | None, zone: str | None) -> str:
    gps = "VALIDO" if gps_valid is True else "INVALIDO" if gps_valid is False else "N/A"
    return (
        f"[POS] [{origin}] [{collar_label(collar_id)}] seq={value_text(sequence)} GPS={gps} "
        f"HDOP={_number(hdop if gps_valid is not False else None, '.2f')} "
        f"RSSI={_number(rssi, 'g', 'dBm')} SNR={_number(snr, '.2f', 'dB')} "
        f"ZONA={value_text(zone if gps_valid is not False else None)}"
    )


def event_block(origin: str, collar_id: int | str | None, name: str,
                details: str = "") -> str:
    alert = name in ALERT_EVENTS
    rule = ALERT_RULE if alert else RULE
    return (
        f"{rule}\n[{'ALERTA' if alert else 'EVENTO'}] [{origin}] [{collar_label(collar_id)}] "
        f"{value_text(name)}{(' ' + details) if details else ''}\n{rule}"
    )


def metric_identity(summary: MetricSummary) -> str:
    return (
        f"[{collar_label(summary.collar_id)}] tipo={summary.kind} "
        f"produtor={value_text(summary.producer_id)} sessao={summary.session_id} janela={summary.window_id}"
    )


def metric_block(origin: str, summary: MetricSummary, result: str) -> str:
    clock = "da coleira" if summary.kind == "HDOP" else "do Gateway"
    valid, values = _metric_values(summary)
    return (
        f"{RULE}\n VFENCE | {origin} | {summary.kind} 60s | {collar_label(summary.collar_id)}\n"
        f" Sessao: {summary.session_id} | Janela: {summary.window_id}\n"
        f" Inicio: {summary.started_at_ms} ms (relogio {clock}) | Duracao: {summary.duration_ms} ms\n"
        f" Amostras: {summary.sample_count} | Validade: {'VALIDA' if valid else 'INVALIDA'}\n"
        f" {values}\n{result}\n{RULE}"
    )


def _mean(value: int | None, valid: bool, unit: str = "") -> str:
    return _number(value / 100 if valid and value is not None else None, ".2f", unit)


def _metric_values(summary: MetricSummary) -> tuple[bool, str]:
    if summary.kind == "HDOP":
        valid = summary.hdop_mean_valid is True
        return valid, f"HDOP medio: {_mean(summary.hdop_mean_x100, valid)}"
    rssi_valid = summary.rssi_mean_valid is True
    snr_valid = summary.snr_mean_valid is True
    rssi = _mean(summary.rssi_mean_x100, rssi_valid, " dBm")
    snr = _mean(summary.snr_mean_x100, snr_valid, " dB")
    return rssi_valid and snr_valid, f"RSSI medio: {rssi} | SNR medio: {snr}"


def base_value(value: object, *, limit: int = 240) -> str:
    if value is None:
        return "N/A"
    text = str(value)
    return text[:limit].translate(_CONTROL_CHARS) + ("..." if len(text) > limit else "")


def gateway_log_field(name: str, value: object) -> str | int | float | bool | None:
    """Accept only scalar protocol fields with known domains for terminal output."""
    if name in _GATEWAY_LOG_ENUMS:
        return value if type(value) is str and value in _GATEWAY_LOG_ENUMS[name] else None
    if name in _GATEWAY_LOG_NUMBERS:
        if type(value) is int and -(2**63) <= value < 2**64:
            return value
        if name in {"latitude", "longitude", "hdop", "rssi", "snr"} and type(value) is float and math.isfinite(value):
            return value
    if name in _GATEWAY_LOG_FLAGS and type(value) is bool:
        return value
    if name in {"collarSessionId", "gatewaySessionId", "windowId"} and type(value) is str:
        return value if re.fullmatch(r"[0-9a-f]{16}", value) else None
    return None


def gateway_log_snapshot(data: dict) -> dict:
    """Keep the logging copy independent of the data used by the application."""
    return {
        name: safe_value for name, value in data.items()
        if (safe_value := gateway_log_field(name, value)) is not None
    }


def base_block(title: str, *rows: str, collar_id: int | str | None = None,
               critical: bool = False) -> str:
    return terminal_block("BASE", title, *rows, collar_id=collar_id, critical=critical)


def terminal_block(component: str, title: str, *rows: str, collar_id: int | str | None = None,
                   critical: bool = False, banner: bool = False, origin: str | None = None) -> str:
    rule = RULE if banner else ALERT_RULE if critical else BLOCK_RULE
    scope = f"[{component}]" + (f"[{origin}]" if origin else "")
    if collar_id is not None:
        scope += f"[{collar_label(collar_id)}]"
    heading = f"VFENCE | {component} | {title}".center(len(RULE)) if banner else f"{scope} {base_value(title)}"
    lines = [base_value(row, limit=4096) if component == "GATEWAY" else base_value(row) for row in rows]
    if banner:
        return "\n".join(("", rule, heading, rule, *(f" {line}" for line in lines), rule))
    return "\n".join(("", rule, heading, *(f" {line}" for line in lines), rule))


def terminal_emit(logger, render, *args, level=logging.INFO, **kwargs):
    """Failure to render or emit an optional message must not interrupt transport."""
    try:
        logger.log(level, "%s", render(*args, **kwargs))
    except Exception:
        pass


_HUMAN_PREFIX = re.compile(r"^\[([^\]]+)\](?:\s+\[GATEWAY\])?(?:\s+\[COL ([0-9]{3})\])?\s*")
_HUMAN_SECRET = re.compile(
    r"(?i)\b(?:authorization|cookie|password|senha|base[_-]?token|access[_-]?token|"
    r"profile[_-]?control[_-]?token|session[_-]?secret|api[_-]?key|secret|bearer)\b\s*[:=]?\s*\S"
)
_BOOT_DETAIL = ("configsip:", "clk_drv:", "mode:", "load:", "entry ", "ho ")
_RADIO_LINES = ("Frequency:", "SF:", "BW:", "CR:", "Preamble:", "SyncWord:", "TX power:", "CRC:")


def gateway_human_records(rows, *, detail):
    def classify(row):
        normalized = " ".join(base_value(row, limit=4096).split())
        if normalized.startswith(("VFENCE | GATEWAY | INICIALIZACAO", "[INFO] USB Serial:", "[INFO] Exportacao",
                           "[OK] Gateway inicializado")):
            return "INICIALIZACAO", None, logging.INFO, True
        if row.startswith(("--- RADIO CONFIG ---", *_RADIO_LINES)):
            return "CONFIGURACAO DE RADIO INFORMADA NO BANNER", None, logging.INFO, True
        if row.startswith(("rst:", *_BOOT_DETAIL)):
            return "BOOT DO ESP32", None, logging.INFO, False
        match = _HUMAN_PREFIX.match(row)
        if not match:
            return "USB / SERIAL", None, logging.INFO, False
        category, collar = match.groups()
        if category == "AVISO" and "evento duplicado" in row.lower():
            return "EVENTO DUPLICADO", collar, logging.WARNING, False
        title = {"POS": "POSICAO", "INFO": "INFORMACAO"}.get(category, category.upper())
        level = logging.ERROR if category in {"ERRO", "FATAL"} else (
            logging.WARNING if category in {"AVISO", "ALERTA", "NACK LoRa", "RETRY LoRa"} else logging.INFO
        )
        return title, collar, level, False

    def routine_metric(row):
        if re.match(r"^\[(?:TX SERIAL|RETRY|ACK SQLITE)\] \[GATEWAY\] \[COL \d{3}\] tipo=(?:RADIO|HDOP)\b", row):
            return "aguardando METRIC_COMMITTED" in row or "pendencia consumida" in row
        if row.endswith(("/SUBST", "janela anterior sobrescrita")):
            return False
        # The firmware can send the banner and its continuation in separate USB reads.
        return bool(re.match(
            r"^(?:\[(?:RADIO 60s|HDOP(?: 60s)?|PDR 60s)\] \[COL \d{3}\]"
            r"|==== VFENCE \| GATEWAY \| (?:RADIO|HDOP|PDR) 60s \| COL \d{3} ====$"
            r"|sessao=[0-9a-f]{16} janela=[0-9a-f]{16} inicio(?:Gateway|Coleira)=\d+ ms duracao=\d+ ms$"
            r"|amostras=\d+ (?:RSSI=.+ SNR=.+ validade=\w+ \| fila=.+|HDOP=\S+ validade=\w+ \| RX LoRa retido)$"
            r"|rx=\d+ (?:PDR=N/A|tx~\d+ perda~\d+ PDR~[\d.]+%) \S+ (?:P[0-3]|N/A)$)",
            row.strip(),
        ))

    rows = (row for row in rows if row.strip("=-!") and (
        detail or not (row.startswith(_BOOT_DETAIL) or routine_metric(row))
    ))
    for (title, collar, level, banner), group in groupby(rows, classify):
        lines = tuple("[Conteudo sensivel omitido]" if _HUMAN_SECRET.search(row) else row for row in group)
        yield level, terminal_block("GATEWAY", title, *lines, collar_id=collar,
                                    critical=level >= logging.WARNING, banner=banner)


_APP_NUMBERS = _GATEWAY_LOG_NUMBERS | {
    "txFirst", "txLast", "radioCount", "rssiX100", "snrX100", "hdopCount", "hdopX100",
    "gatewayNowMs", "gatewayTickMs", "nextWindowId", "currentProfileId", "targetProfileId",
    "radioSessionAgeMs", "profilesCount", "enabledCount", "elapsedMs", "gatewayMillis",
    "gatewayWindowStartedAtMs", "nextWindowStartedAtMs", "id", "sf",
    "sourceProfileId", "deadlineAtMs", "clockVersion", "tickMs", "windowStartMs", "crDenominator", "txDbm",
    "preambleSymbols", "deliveryVersion",
}
_APP_FLAGS = _GATEWAY_LOG_FLAGS | {"recovered", "proved", "committed", "escaped", "closed", "valid",
                                 "crc", "explicitHeader", "invertedIq", "candidate"}
_APP_ENUMS = {
    **_GATEWAY_LOG_ENUMS,
    "schema": _GATEWAY_LOG_ENUMS["schema"] | {EVIDENCE_SCHEMA},
    "kind": _GATEWAY_LOG_ENUMS["kind"] | {EVIDENCE_KIND},
    "state": _GATEWAY_LOG_ENUMS["state"] | {"PREPARING", "COMMITTING", "VERIFYING", "COMPLETED", "ROLLING_BACK", "FAILED"},
    "origin": {"LoRa", "LORA", "LoRa_RX", "SERIAL", "USB"},
    "type": {"telemetry", "event", "offline"},
    "event": _GATEWAY_LOG_ENUMS["subtype"],
}


def _app_snapshot(data):
    safe = {}
    for key, value in data.items():
        if key in _APP_ENUMS and type(value) is str and value in _APP_ENUMS[key]:
            safe[key] = value
        elif key in _APP_NUMBERS and (type(value) is int and -(2**63) <= value < 2**64
                                      or type(value) is float and math.isfinite(value)):
            safe[key] = value
        elif key in _APP_FLAGS and type(value) is bool:
            safe[key] = value
        elif value is None and (key in _APP_NUMBERS or key in _APP_FLAGS or key in _APP_ENUMS):
            safe[key] = None
        elif key in {"gatewaySessionId", "collarSessionId", "windowId", "deliveryId",
                     "nextWindowId", "firstWindowId", "operationId"} and type(value) is str and re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{24}", value):
            safe[key] = value
        elif key in {"profiles", "participants", "roster", "members"} and isinstance(value, list):
            safe[key] = [_app_snapshot(entry) if isinstance(entry, dict) else entry
                         for entry in value[:254] if isinstance(entry, dict) or type(entry) is int and 1 <= entry <= 254]
    return safe


def app_raw(wire):
    # Never change the original frame. Render only the fields approved for this diagnostic.
    return "[RAW GATEWAY] [APP] " + json.dumps(_app_snapshot(json.loads(wire.removeprefix("[APP] "))),
                                               separators=(",", ":"), allow_nan=False)


def app_level(data):
    name = data.get("subtype") or data.get("event")
    state = data.get("state")
    return logging.WARNING if (data["kind"] == "offline" or data.get("ok") is False or isinstance(name, str) and name in ALERT_EVENTS
                               or isinstance(state, str) and state.upper() in {"FAILED", "OFFLINE"}) else logging.INFO


_APP_FIELDS = {
    "collar_state": ("state", "origin", "lastSeenS", "sequence"),
    "collar_status": ("state", "origin", "lastSeenS", "rssi", "snr", "empty"),
    "offline": ("lastSeenS", "origin"),
    "gateway_state": ("state", "fenceTransferActive", "activeProfileId", "switchEnabled", "gatewaySessionId", "empty"),
    "radio_config": ("frequencyHz", "spreadingFactor", "bandwidthHz", "codingRateDenominator",
                     "preambleLength", "syncWord", "txPowerDbm", "crcEnabled", "activeProfileId"),
    "control": ("subtype", "sequence", "ackedSequence", "errorCode", "detail"),
    "command_response": ("command", "requestId", "ok", "detail", "state", "activeProfileId", "requestedProfileId",
                         "profileControlVersion", "gatewaySessionId", "operationId", "firstWindowId",
                         "gatewayWindowStartedAtMs", "nextWindowId", "gatewayNowMs", "gatewayMillis",
                         "gatewayTickMs", "nextWindowStartedAtMs", "elapsedMs", "profiles", "participants", "roster", "members",
                         "sourceProfileId", "targetProfileId", "deadlineAtMs", "clockVersion", "tickMs",
                         "windowStartMs", "windowId", "durationMs", "switchEnabled"),
    "fence_transfer": ("state", "requestId", "fenceVersion", "vertexCount", "detail"),
    "hdop_window_summary": ("windowId", "durationMs", "sampleCount", "hdopMeanX100", "hdopMeanValid"),
    "radio_window_summary": ("windowId", "durationMs", "sampleCount", "rssiMeanX100", "rssiMeanValid", "snrMeanX100", "snrMeanValid"),
    "pdr_window_summary": ("windowId", "durationMs", "profileId", "receivedCount", "estimatedTxCount", "estimatedLostCount",
                           "pdrX100", "valid", "reason"),
    EVIDENCE_KIND: ("windowId", "durationMs", "profileId", "txFirst", "txLast", "receivedCount",
                    "pdrX100", "radioCount", "rssiX100", "snrX100", "hdopCount", "hdopX100"),
}
_APP_LABELS = {
    "state": "Estado", "origin": "Origem", "lastSeenS": "Ultimo contato", "sequence": "Sequencia",
    "frequencyHz": "Frequencia ativa", "spreadingFactor": "SF", "bandwidthHz": "BW", "codingRateDenominator": "CR",
    "preambleLength": "Preamble", "syncWord": "SyncWord", "txPowerDbm": "Potencia TX", "crcEnabled": "CRC",
    "activeProfileId": "Perfil ativo", "profileId": "Perfil da janela", "ok": "Aceito", "requestId": "Request ID",
    "subtype": "Tipo", "detail": "Detalhe", "fenceVersion": "Versao da cerca", "windowId": "Janela",
    "durationMs": "Duracao", "sampleCount": "Amostras", "hdopMeanX100": "HDOP medio", "hdopX100": "HDOP medio",
    "rssiMeanX100": "RSSI medio", "rssiX100": "RSSI medio", "snrMeanX100": "SNR medio", "snrX100": "SNR medio",
    "pdrX100": "PDR", "receivedCount": "Recebimentos", "estimatedTxCount": "Transmissoes estimadas",
    "radioCount": "Amostras de radio", "hdopCount": "Amostras de HDOP", "reason": "Motivo",
    "command": "Comando", "gatewaySessionId": "Sessao Gateway", "participants": "Participantes",
    "sourceProfileId": "Perfil de origem", "targetProfileId": "Perfil de destino", "tickMs": "Relogio Gateway",
    "windowStartMs": "Inicio da janela", "deadlineAtMs": "Prazo no relogio Gateway",
}
_APP_TITLES = dict(zip(_APP_FIELDS, (
    "ESTADO DA COLEIRA", "CONSULTA DE COLEIRA", "COLEIRA OFFLINE", "ESTADO DO GATEWAY",
    "CONFIGURACAO DE RADIO ATIVA", "CONTROLE LORA", "RESPOSTA DE COMANDO", "DISTRIBUICAO DE CERCA",
    "RESUMO HDOP", "RESUMO RADIO", "PDR ESTIMADO", "EVIDENCIA DE PDR COMPROVADO",
)))


def _scaled(value, unit=""):
    return _number(value / 100 if type(value) in (int, float) else None, ".2f", unit)


def _app_field_value(key, value):
    if value is None:
        return "N/A"
    if type(value) is bool:
        return ("ON" if value else "OFF") if key == "crcEnabled" else ("SIM" if value else "NAO")
    if key in {"frequencyHz", "bandwidthHz", "durationMs", "lastSeenS", "txPowerDbm", "rssi", "snr",
               "tickMs", "windowStartMs", "deadlineAtMs"}:
        unit = {"frequencyHz": " Hz", "bandwidthHz": " Hz", "durationMs": " ms", "lastSeenS": " s",
                "txPowerDbm": " dBm", "rssi": " dBm", "snr": " dB",
                "tickMs": " ms", "windowStartMs": " ms", "deadlineAtMs": " ms"}[key]
        return _number(value, ".2f" if key == "snr" else "g", unit)
    if key in {"hdopMeanX100", "hdopX100", "rssiMeanX100", "rssiX100", "snrMeanX100", "snrX100", "pdrX100"}:
        unit = " dBm" if key.startswith("rssi") else " dB" if key.startswith("snr") else "%" if key == "pdrX100" else ""
        return _scaled(value, unit)
    if key in {"activeProfileId", "profileId", "requestedProfileId", "sourceProfileId", "targetProfileId"}:
        return f"P{value}"
    if key == "syncWord" and type(value) is int:
        return f"0x{value:02X}"
    if key == "codingRateDenominator":
        return f"4/{value}"
    if isinstance(value, list):
        if key == "profiles":
            return ", ".join("P" + str(entry.get("id", "N/A")) for entry in value if isinstance(entry, dict)) or "Nenhum"
        return ", ".join(collar_label(entry.get("collarId")) + (
            f" (prova: {'SIM' if entry['proved'] else 'NAO'})" if "proved" in entry else ""
        ) if isinstance(entry, dict) else collar_label(entry) for entry in value) or "Nenhum"
    return base_value(value)


def app_message(data, *, packet=None):
    kind = data["kind"]
    safe = _app_snapshot(data)
    value = lambda key: base_value(safe.get(key))
    title = _APP_TITLES.get(kind, "QUADRO RECEBIDO")
    critical = app_level(data) >= logging.WARNING
    if kind == "telemetry" and packet is not None:
        valid = safe.get("gpsValid") is True
        title = "TELEMETRIA"
        rows = [
            f"Sequencia: {value('sequence')}", f"GPS: {'VALIDO' if valid else 'INVALIDO' if safe.get('gpsValid') is False else 'N/A'}",
            f"Latitude: {_number(packet.latitude, '.7f')}", f"Longitude: {_number(packet.longitude, '.7f')}",
            f"Distancia da borda: {_number(safe.get('distanceBorderM') if valid else None, 'g', ' m')}",
            f"Bateria: {_number(safe.get('battery'), 'g', '%')} | Satelites: {_number(packet.satellites, 'g')}",
            f"HDOP: {_number(packet.hdop, '.2f')}",
            f"RSSI: {_number(packet.rssi, 'g', ' dBm')} | SNR: {_number(packet.snr, '.2f', ' dB')}",
            f"Zona: {base_value(packet.zone if valid else None)}", f"Versao da cerca: {value('fenceVersion')}",
        ]
    elif kind == "event":
        title = "EVENTO"
        name = safe.get("subtype") or safe.get("event")
        critical = name in ALERT_EVENTS
        rows = [f"Tipo: {base_value(name)}"]
        for key, label, spec, unit in (("latitude", "Latitude", ".7f", ""), ("longitude", "Longitude", ".7f", ""),
                                       ("distanceBorderM", "Distancia da borda", "g", " m")):
            if key in safe:
                rows.append(f"{label}: {_number(safe[key], spec, unit)}")
        rows.extend(f"{_APP_LABELS.get(key, key)}: {_app_field_value(key, safe[key])}"
                    for key in ("sequence", "fenceVersion", "zone", "battery", "satellites", "hdop", "rssi", "snr", "origin")
                    if key in safe)
        if "recovered" in safe:
            rows.append(f"Recuperacao do GPS: {_app_field_value('recovered', safe['recovered'])}")
    else:
        if kind == "command_response" and safe.get("command") == "GET_METRIC_CLOCK_V1":
            title = "SINCRONIZACAO TEMPORAL"
        elif kind == "command_response" and "PROFILE" in str(safe.get("command")):
            title = "CONTROLE DE PERFIS | RESPOSTA"
        rows = [f"{_APP_LABELS.get(key, key)}: {_app_field_value(key, safe[key])}"
                for key in _APP_FIELDS.get(kind, ()) if key in safe]
        if kind == "offline":
            rows.insert(0, "Estado: OFFLINE")
        if kind == EVIDENCE_KIND:
            rows.append("Estado: EVIDENCIA RECEBIDA | PERSISTENCIA AINDA NAO CONFIRMADA")
    return terminal_block("APP", title, *rows, collar_id=safe.get("collarId"), critical=critical, origin="GATEWAY")


def evidence_block(summary: MetricEvidence, result: str):
    return base_block(
        "PDR COMPROVADO | EVIDENCIA DA JANELA",
        f"Janela Gateway: {summary.window_id} | Duracao: 60 s | Perfil: P{summary.profile_id}",
        f"Transmissoes: {summary.transmitted} | Recebimentos: {summary.received}",
        f"PDR comprovado: {_scaled(summary.pdr, '%')}",
        f"RSSI medio: {_scaled(summary.rssi, ' dBm')} | SNR medio: {_scaled(summary.snr, ' dB')}",
        f"HDOP medio: {_scaled(summary.hdop)} | Radio: {summary.radio_count} amostras | HDOP: {summary.hdop_count} amostras",
        f"SQLite: {_SQLITE_RESULTS[result]}",
        collar_id=summary.collar_id, critical=result not in {"INSERTED", "DUPLICATE"},
    )


def metric_window_block(summary: MetricEvidence, arrival: str | None):
    values = (summary.rssi, summary.snr, summary.pdr, summary.hdop)
    valid = sum(value is not None for value in values)
    rows = [
        f"Janela: {summary.window_id}", f"Perfil: P{summary.profile_id}",
        f"RSSI medio: {_scaled(summary.rssi, ' dBm') if summary.rssi is not None else 'INDISPONIVEL'}",
        f"SNR medio: {_scaled(summary.snr, ' dB') if summary.snr is not None else 'INDISPONIVEL'}",
        f"PDR comprovado: {_scaled(summary.pdr, '%') if summary.pdr is not None else 'INDISPONIVEL'}",
        f"HDOP medio: {_scaled(summary.hdop) if summary.hdop is not None else 'INDISPONIVEL'}",
        f"Amostras de radio: {summary.radio_count}", f"Amostras de HDOP: {summary.hdop_count}",
        f"Metricas validas: {valid}/4",
    ]
    if valid != 4:
        rows.append("Estado: METRICAS INCOMPLETAS")
    if arrival:
        rows.append(f"Recepcao: {arrival} | Inicio original (Gateway): {summary.started_at_ms} ms")
    return terminal_block(collar_label(summary.collar_id), "RESUMO DE METRICAS 60s", *rows,
                          "SQLite: COMMIT CONCLUIDO", banner=True)


class MetricTerminalView:
    """Bounded display context. The database still decides insert, duplicate and conflict."""

    def __init__(self):
        self._proofs = OrderedDict()
        self._arrivals = OrderedDict()
        self._clock = None

    @staticmethod
    def _remember(records, key, value):
        records[key] = value
        records.move_to_end(key)
        if len(records) > 256:
            records.popitem(last=False)

    def observe_clock(self, data, arrival_time):
        session, window = data.get("gatewaySessionId"), data.get("windowId")
        if (data.get("ok") is True and data.get("clockVersion") == 1 and data.get("durationMs") == 60000
                and isinstance(session, str) and re.fullmatch(r"[0-9a-f]{16}", session)
                and isinstance(window, str) and re.fullmatch(r"[0-9a-f]{16}", window)):
            key = session, int(window, 16), arrival_time
            if self._clock is None or self._clock[0] != session or key[1] >= self._clock[1]:
                self._clock = key

    def remember_proof(self, gateway, installation, proof, *, inserted, arrival_time):
        scope = (gateway, installation, proof.gateway_session_id, proof.collar_id)
        self._remember(self._proofs, (*scope, proof.window_id), proof)
        if not inserted:
            return None
        stream = (*scope, proof.collar_session_id)
        window = int(proof.window_id, 16)
        previous = self._arrivals.get(stream)
        late = (previous is not None and window < previous[0]) or (
            self._clock is not None and self._clock[0] == proof.gateway_session_id
            and self._clock[1] >= window + 2 and self._clock[2] <= arrival_time
        )
        burst = previous is not None and 0 <= arrival_time - previous[1] <= 1
        self._remember(self._arrivals, stream, (max(window, previous[0]) if previous else window, arrival_time))
        if late and burst:
            return "JANELA ATRASADA | CHEGADA EM RAJADA"
        if late:
            return "JANELA ATRASADA"
        return "CHEGADA EM RAJADA" if burst else None

    def covers_summary(self, gateway, installation, summary):
        session = summary.session_id if summary.kind == "RADIO" else summary.gateway_session_id
        proof = self._proofs.get((gateway, installation, session, summary.collar_id, summary.window_id))
        if proof is None or summary.duration_ms != 60000:
            return False
        if summary.kind == "RADIO":
            return (summary.producer_id == gateway and summary.started_at_ms == proof.started_at_ms
                    and (summary.sample_count, summary.rssi_mean_x100, summary.snr_mean_x100)
                    == (proof.radio_count, proof.rssi, proof.snr))
        return (summary.producer_id == installation and summary.session_id == proof.collar_session_id
                and (summary.sample_count, summary.hdop_mean_x100) == (proof.hdop_count, proof.hdop))

    def covers_pdr(self, gateway, installation, summary):
        proof = self._proofs.get((gateway, installation, summary.gateway_session_id, summary.collar_id, summary.window_id))
        return (proof is not None and summary.collar_session_id == proof.collar_session_id
                and summary.profile_id == proof.profile_id and summary.started_at_ms == proof.started_at_ms
                and summary.duration_ms == 60000)


def correlated_metrics(pdr_row, radio, hdop, sample: FpgaSample):
    proven = pdr_row.get("tx_proven") == 1
    total = pdr_row.get("transmitted_count") if proven else pdr_row.get("estimated_tx_count")
    rows = [
        f"Janela Gateway: {pdr_row['window_id']} | Duracao: {pdr_row['duration_ms'] // 1000} s",
        f"Perfil: P{pdr_row['profile_id']}",
        f"{'Transmissoes' if proven else 'Transmissoes estimadas'}: {base_value(total)} | Recebimentos: {pdr_row['received_count']}",
        f"PDR {'comprovado' if proven else 'estimado'}: {_scaled(sample.pdr_x100, '%')}",
        f"RSSI medio: {_scaled(radio.rssi_mean_x100, ' dBm') if radio and sample.rssi_mag is not None else 'N/A'}",
        f"SNR medio: {_scaled(sample.snr_x100, ' dB')}",
        f"HDOP medio: {_scaled(sample.hdop_x100) if hdop and sample.hdop_x100 is not None else 'INDISPONIVEL'}",
        f"Amostras de radio: {base_value(sample.sample_count)}",
        "SQLite: REGISTROS PERSISTIDOS | Estado: CORRELACAO ACEITA NA JANELA",
    ]
    return base_block("METRICAS CORRELACIONADAS | FPGA", *rows, collar_id=pdr_row["collar_id"])


def fpga_sample_block(sample: FpgaSample, profile: int, response: str | None):
    metrics = (("HDOP", sample.hdop_x100), ("PDR", sample.pdr_x100), ("SNR", sample.snr_x100), ("RSSI", sample.rssi_mag))
    missing = [f"{label} INDISPONIVEL" for label, value in metrics if value is None]
    if not sample.sample_count:
        missing.append("SEM AMOSTRAS DE RADIO")
    mask = sample.metrics_valid
    if response == "OK|SAMPLE":
        status = "AMOSTRA ACEITA"
    elif response is None:
        status = "ACEITACAO DA AMOSTRA NAO CONFIRMADA"
    elif response.startswith("VFENCE|P"):
        status = "RESULTADO RETORNADO | ACEITACAO INDIVIDUAL NAO CONFIRMADA"
    else:
        status = f"ACEITACAO NAO CONFIRMADA | {base_value(response)}"
    label = "enviado" if response is not None else "previsto"
    rows = [
        f"Perfil atual: P{profile}",
        f"RSSI {label}: {_number(-sample.rssi_mag if sample.rssi_mag is not None else None, 'g', ' dBm')}",
        f"SNR {label}: {_scaled(sample.snr_x100, ' dB')}",
        f"PDR {label}: {_scaled(sample.pdr_x100, '%')}",
        f"HDOP: {_scaled(sample.hdop_x100) if sample.hdop_x100 is not None else 'INDISPONIVEL'}",
        f"Amostras: {base_value(sample.sample_count)}", f"Metricas validas: {mask.bit_count()}/4",
        f"Mascara: {mask:04b} ({mask})", f"Nios II: {status}",
        f"Elegibilidade normal: {'ELEGIVEL SEGUNDO OS CRITERIOS VERIFICAVEIS' if sample.valid_vote else 'INCOMPLETA'}",
    ]
    if missing:
        rows.append("Motivo: " + " | ".join(missing))
        if sample.pdr_x100 is not None:
            rows.append("PDR disponivel para o mecanismo independente de alerta critico")
    rows.append("Voto individual no RTL: NAO CONFIRMADO")
    return terminal_block("FPGA", f"AMOSTRA | MINUTO {sample.minute_index}", *rows, collar_id=sample.collar_id,
                          critical=response != "OK|SAMPLE")


_DECISION_LABELS = {
    0: "MANUTENCAO DE PERFIL", 1: "AUMENTO DE PERFIL", 2: "REDUCAO DE PERFIL",
    3: "ALERTA CRITICO | PERFIL MANTIDO", 4: "VOTOS INSUFICIENTES", 5: "EMPATE",
}


def fpga_result_block(result, previous, auto_enabled):
    return terminal_block(
        "FPGA", "RESULTADO",
        f"Janela: {result['window_id']}", f"Perfil anterior: P{previous}",
        f"Perfil recomendado: P{result['profile']}", f"Motivo: {_DECISION_LABELS[result['reason']]}",
        f"Codigo de decisao: {result['reason']}", f"Alerta critico: {'SIM' if result['critical'] else 'NAO'}",
        f"Troca automatica: {'HABILITADA' if auto_enabled else 'DESATIVADA'}",
        "Estado: VOTACAO ENCERRADA | RECOMENDACAO RECEBIDA",
        banner=True,
    )


def base_position(collar_id: str, sequence: int | None, gps_valid: bool | None,
                  hdop: float | None, rssi: float | None, snr: float | None,
                  zone: str | None, *, detail: bool) -> str:
    gps = "OK" if gps_valid is True else "INDISPONIVEL" if gps_valid is False else "N/A"
    location = base_value(zone if gps_valid is not False else None)
    dilution = _number(hdop if gps_valid is not False else None, ".2f")
    rows = [
        f"GPS: {gps} | Zona: {location} | HDOP: {dilution}",
        f"RSSI: {_number(rssi, 'g', ' dBm')} | SNR: {_number(snr, '.2f', ' dB')}",
    ]
    if detail:
        rows.append(f"Sequencia: {base_value(sequence)}")
    return base_block("POSICAO | SQLite: SALVA", *rows, collar_id=collar_id)


def base_event(collar_id: str | None, name: str, *, source: str,
               sequence: object, fence_version: object, detail: bool, sync_enabled: bool) -> str:
    critical = name in ALERT_EVENTS
    rows = [f"Origem: {source} | Estado: EVENTO RECEBIDO"]
    if detail and (sequence is not None or fence_version is not None):
        rows.append(f"Sequencia: {base_value(sequence)} | Cerca: {base_value(fence_version)}")
    sync = "AGUARDANDO SINCRONIZACAO" if sync_enabled else "SINCRONIZACAO DESATIVADA"
    rows.append(f"SQLite: COMMIT CONCLUIDO | Central: {sync}")
    return base_block(f"{'ALERTA' if critical else 'EVENTO'} | {name}", *rows,
                      collar_id=collar_id, critical=critical)


def base_metric(summary: MetricSummary, result: str, *, detail: bool) -> str:
    hdop = summary.kind == "HDOP"
    valid, values = _metric_values(summary)
    validity = "VALIDA" if valid else "INVALIDA"
    if not summary.sample_count:
        validity += " | Motivo: SEM AMOSTRAS"
    rows = [f"Janela: {summary.window_id} | Resumo avulso", values,
            f"Amostras: {summary.sample_count} | Validade: {validity}", f"SQLite: {_SQLITE_RESULTS[result]}"]
    if detail:
        clock = "coleira" if hdop else "Gateway"
        rows.extend((
            f"Produtor: {base_value(summary.producer_id, limit=64)} | Sessao: {summary.session_id} | Janela: {summary.window_id}",
            f"Inicio ({clock}): {summary.started_at_ms} ms | Duracao: {summary.duration_ms} ms",
        ))
        if hdop and summary.gateway_session_id is not None:
            rows.append(f"Sessao Gateway: {summary.gateway_session_id} | RX Gateway: {summary.gateway_received_at_ms} ms")
    return base_block(f"{summary.kind} | JANELA 60s", *rows, collar_id=summary.collar_id,
                      critical=result in {"CONFLICT", "FAILED"})


def base_pdr(summary: PdrSummary, result: str, *, detail: bool) -> str:
    profile = f"P{summary.profile_id}" if summary.profile_id is not None else "N/A"
    percent = _mean(summary.pdr_x100, summary.valid, "%")
    validity = "VALIDO NO INTERVALO OBSERVADO" if summary.valid else f"INSUFICIENTE | Motivo: {summary.reason}"
    rows = [
        f"Janela: {summary.window_id} | Resumo avulso",
        f"Entrega: {percent} | Perfil: {profile}",
        f"Recebidos: {summary.received_count} | Intervalo estimado: {base_value(summary.estimated_tx_count)}",
        f"Validade: {validity} | SQLite: {_SQLITE_RESULTS[result]}",
    ]
    if detail:
        rows.extend((
            f"Sessao Gateway: {summary.gateway_session_id} | Sessao coleira: {base_value(summary.collar_session_id)}",
            f"Janela: {summary.window_id} | Inicio (Gateway): {summary.started_at_ms} ms | Duracao: {summary.duration_ms} ms",
            f"Perdas estimadas: {base_value(summary.estimated_lost_count)}",
        ))
    return base_block("PDR ESTIMADO | JANELA 60s", *rows, collar_id=summary.collar_id,
                      critical=result in {"CONFLICT", "FAILED"})


def _fpga_field(value: int | None, valid: bool, minimum: int, maximum: int) -> str:
    if not valid:
        return "INVALIDO"
    if value is None:
        return "AUSENTE"
    return "DISPONIVEL" if minimum <= value <= maximum else "INVALIDO"


def fpga_metric_diagnostic(summary: MetricSummary | PdrSummary, received_at: datetime,
                           *, fpga_enabled: bool = False) -> str:
    fields = dict.fromkeys(
        ("hdop_x100", "rssi_mag", "snr_x100", "pdr_x100", "sample_count"), "AUSENTE",
    )
    clock = "Gateway"
    if isinstance(summary, PdrSummary):
        kind = "PDR"
        session = summary.gateway_session_id
        values = (
            f"pdrX100={base_value(summary.pdr_x100)} | PDR estimado={_mean(summary.pdr_x100, summary.valid, '%')}"
            f" | valid={summary.valid} | reason={summary.reason}\n"
            f" receivedCount={summary.received_count} | estimatedTxCount={base_value(summary.estimated_tx_count)}"
            f" | estimatedLostCount={base_value(summary.estimated_lost_count)}"
            f" | profileId={base_value(summary.profile_id)} | collarSessionId={base_value(summary.collar_session_id)}"
        )
        fields["pdr_x100"] = _fpga_field(summary.pdr_x100, summary.valid, 1, 10000)
    else:
        kind = summary.kind
        session = summary.session_id
        _, means = _metric_values(summary)
        values = f"sampleCount={summary.sample_count} | {means}"
        if kind == "HDOP":
            clock = "coleira"
            values += (
                f"\n hdopMeanX100={base_value(summary.hdop_mean_x100)} | hdopMeanValid={summary.hdop_mean_valid}"
                f" | gatewaySessionId={base_value(summary.gateway_session_id)}"
                f" | gatewayReceivedAtMs={base_value(summary.gateway_received_at_ms)}"
            )
            fields["hdop_x100"] = _fpga_field(
                summary.hdop_mean_x100, summary.hdop_mean_valid is True and 1 <= summary.sample_count <= 60, 0, 65534,
            )
        else:
            values += (
                f"\n rssiMeanX100={base_value(summary.rssi_mean_x100)} | rssiMeanValid={summary.rssi_mean_valid}"
                f" | snrMeanX100={base_value(summary.snr_mean_x100)} | snrMeanValid={summary.snr_mean_valid}"
            )
            fields["rssi_mag"] = _fpga_field(summary.rssi_mean_x100, summary.rssi_mean_valid is True, -25500, 0)
            fields["snr_x100"] = _fpga_field(summary.snr_mean_x100, summary.snr_mean_valid is True, -32768, 32767)
            fields["sample_count"] = _fpga_field(summary.sample_count, True, 1, 255)

    availability = " | ".join(f"{field}={state}" for field, state in fields.items())
    flags = " | ".join(
        f"{name}={fields[field]}"
        for name, field in (("HDOP", "hdop_x100"), ("PDR", "pdr_x100"), ("SNR", "snr_x100"), ("RSSI", "rssi_mag"))
    )
    return (
        f"[BASE][{collar_label(summary.collar_id)}] DADOS RECEBIDOS | {kind} | Sessao={session}"
        f" | Janela={summary.window_id}\n"
        f" RX Base (UTC)={received_at.isoformat(timespec='milliseconds')} | Inicio ({clock})={summary.started_at_ms} ms"
        f" | Duracao={summary.duration_ms} ms\n"
        f" {values}\n"
        f" PREVIA FPGA | Campos neste pacote | collar_id=DISPONIVEL | current_profile=AUSENTE | minute_index=AUSENTE\n"
        f" {availability}\n"
        f" metrics_valid | {flags}\n"
        " Resultado da previa: DADOS INSUFICIENTES | Janelas sem correlacao operacional\n"
        + (" NAO ENVIADO — AGUARDANDO CORRELACAO DA JANELA" if fpga_enabled
           else " NAO ENVIADO — AVALIACAO FPGA DESATIVADA")
    )
