"""Human-readable diagnostics shared by the Base and the PC Serial client."""

from __future__ import annotations

import math
import re

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
    rule = ALERT_RULE if critical else BLOCK_RULE
    scope = f"[BASE][{collar_label(collar_id)}]" if collar_id is not None else "[BASE]"
    return "\n".join(("", rule, f"{scope} {base_value(title)}",
                       *(f" {base_value(row)}" for row in rows), rule))


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
    rows = [values, f"Amostras: {summary.sample_count} | Validade: {validity}", f"SQLite: {_SQLITE_RESULTS[result]}"]
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
