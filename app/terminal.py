"""Human-readable diagnostics shared by the Base and the PC Serial client."""

from __future__ import annotations

import math
import re

from app.metric_protocol import MetricSummary


RULE = "=" * 56
ALERT_RULE = "!" * 56
ALERT_EVENTS = {"OUTSIDE", "GPS_LOST", "LOW_BATTERY", "CRITICAL_BATTERY", "OFFLINE"}


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


def zone_transition_alert(collar_id: str, previous: str, current: str) -> str:
    return (
        f"{ALERT_RULE}\n[ALERTA] [BASE] [{collar_label(collar_id)}] TRANSICAO DE ZONA\n"
        f" {value_text(previous)} -> {value_text(current)}\n"
        f" Origem: telemetria persistida\n{ALERT_RULE}"
    )


def metric_identity(summary: MetricSummary) -> str:
    return (
        f"[{collar_label(summary.collar_id)}] tipo={summary.kind} "
        f"produtor={value_text(summary.producer_id)} sessao={summary.session_id} janela={summary.window_id}"
    )


def metric_block(origin: str, summary: MetricSummary, result: str) -> str:
    hdop = summary.kind == "HDOP"
    clock = "da coleira" if hdop else "do Gateway"
    if hdop:
        valid = summary.hdop_mean_valid is True
        mean = _mean(summary.hdop_mean_x100, valid)
        values = f" HDOP medio: {mean}"
    else:
        valid = summary.rssi_mean_valid is True and summary.snr_mean_valid is True
        rssi = _mean(summary.rssi_mean_x100, summary.rssi_mean_valid is True, " dBm")
        snr = _mean(summary.snr_mean_x100, summary.snr_mean_valid is True, " dB")
        values = f" RSSI medio: {rssi} | SNR medio: {snr}"
    return (
        f"{RULE}\n VFENCE | {origin} | {summary.kind} 60s | {collar_label(summary.collar_id)}\n"
        f" Sessao: {summary.session_id} | Janela: {summary.window_id}\n"
        f" Inicio: {summary.started_at_ms} ms (relogio {clock}) | Duracao: {summary.duration_ms} ms\n"
        f" Amostras: {summary.sample_count} | Validade: {'VALIDA' if valid else 'INVALIDA'}\n"
        f"{values}\n{result}\n{RULE}"
    )


def _mean(value: int | None, valid: bool, unit: str = "") -> str:
    return _number(value / 100 if valid and value is not None else None, ".2f", unit)
