from __future__ import annotations

import re
from dataclasses import dataclass


HDOP_SUMMARY_KIND = "hdop_window_summary"
RADIO_SUMMARY_KIND = "radio_window_summary"
METRIC_SUMMARY_KINDS = frozenset({HDOP_SUMMARY_KIND, RADIO_SUMMARY_KIND})
METRIC_LINE_MAX_BYTES = 512

_HEX_ID = re.compile(r"[0-9a-f]{16}")
_U32_MAX = 0xFFFFFFFF
_COMMON_FIELDS = {"schema", "kind", "collarId", "windowId", "durationMs", "sampleCount"}
_HDOP_FIELDS = {"collarSessionId", "collarStartedAtMs", "hdopMeanX100", "hdopMeanValid"}
_HDOP_GATEWAY_FIELDS = {"gatewaySessionId", "gatewayReceivedAtMs"}
_RADIO_FIELDS = {
    "gatewaySessionId", "gatewayStartedAtMs", "rssiMeanX100", "snrMeanX100",
    "rssiMeanValid", "snrMeanValid",
}


@dataclass(frozen=True, slots=True)
class MetricSummary:
    kind: str
    producer_id: str
    collar_id: int
    session_id: str
    window_id: str
    started_at_ms: int
    duration_ms: int
    sample_count: int
    hdop_mean_x100: int | None = None
    rssi_mean_x100: int | None = None
    snr_mean_x100: int | None = None
    hdop_mean_valid: bool | None = None
    rssi_mean_valid: bool | None = None
    snr_mean_valid: bool | None = None
    gateway_session_id: str | None = None
    gateway_received_at_ms: int | None = None


def _integer(value, key: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} deve ser inteiro entre {minimum} e {maximum}")


def _hex_id(value, key: str) -> None:
    if type(value) is not str or not _HEX_ID.fullmatch(value):
        raise ValueError(f"{key} deve ter 16 digitos hexadecimais minusculos")


def _mean(value, flag, key: str, count: int, minimum: int, maximum: int) -> None:
    if flag is not (count != 0):
        raise ValueError(f"flag de {key} deve ser booleana e corresponder a sampleCount")
    if count:
        _integer(value, key, minimum, maximum)
    elif value is not None:
        raise ValueError(f"{key} deve ser null em janela vazia")


def validate_metric_summary(summary: MetricSummary, *, require_producer: bool = True) -> None:
    if not isinstance(summary, MetricSummary) or type(summary.kind) is not str or summary.kind not in ("HDOP", "RADIO"):
        raise ValueError("tipo de resumo invalido")
    producer = summary.producer_id
    if type(producer) is not str or "\0" in producer or (require_producer and not producer.strip()):
        raise ValueError("identidade do produtor invalida")

    _integer(summary.collar_id, "collarId", 1, 254)
    _hex_id(summary.session_id, "sessionId")
    _hex_id(summary.window_id, "windowId")
    if summary.window_id == "0000000000000000":
        raise ValueError("windowId deve ser diferente de zero")
    _integer(summary.started_at_ms, "startedAtMs", 0, _U32_MAX)
    _integer(summary.duration_ms, "durationMs", 60000, 60000)
    count = summary.sample_count
    if summary.kind == "HDOP":
        _integer(count, "sampleCount", 0, 60)
        _mean(summary.hdop_mean_x100, summary.hdop_mean_valid, "hdopMeanX100", count, 0, 65534)
        if any(value is not None for value in (summary.rssi_mean_x100, summary.snr_mean_x100,
                                               summary.rssi_mean_valid, summary.snr_mean_valid)):
            raise ValueError("resumo HDOP nao pode conter metricas de radio")
        if summary.gateway_session_id is not None or summary.gateway_received_at_ms is not None:
            _hex_id(summary.gateway_session_id, "gatewaySessionId")
            _integer(summary.gateway_received_at_ms, "gatewayReceivedAtMs", 0, _U32_MAX)
    else:
        _integer(count, "sampleCount", 0, 65535)
        _mean(summary.rssi_mean_x100, summary.rssi_mean_valid, "rssiMeanX100", count, -32700, 32700)
        _mean(summary.snr_mean_x100, summary.snr_mean_valid, "snrMeanX100", count, -32768, 32767)
        if any(value is not None for value in (summary.hdop_mean_x100, summary.hdop_mean_valid,
                                               summary.gateway_session_id, summary.gateway_received_at_ms)):
            raise ValueError("resumo RADIO nao pode conter campos exclusivos de HDOP")


def _check_fields(data: dict, required: set[str], optional: set[str] | frozenset[str] = frozenset()) -> None:
    missing = required - data.keys()
    extra = data.keys() - required - optional
    if missing:
        raise ValueError(f"campos de resumo ausentes: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"campos de resumo desconhecidos: {', '.join(sorted(extra))}")


def parse_metric_summary(data: dict, producer_id: str) -> MetricSummary:
    """Validate the fields of a recognized metric envelope and map them to SQLite."""
    kind = data["kind"]
    common = {
        "producer_id": producer_id, "collar_id": data["collarId"], "window_id": data["windowId"],
        "duration_ms": data["durationMs"], "sample_count": data["sampleCount"],
    }
    if kind == HDOP_SUMMARY_KIND:
        _check_fields(data, _COMMON_FIELDS | _HDOP_FIELDS, _HDOP_GATEWAY_FIELDS)
        if _HDOP_GATEWAY_FIELDS & data.keys() and any(data.get(key) is None for key in _HDOP_GATEWAY_FIELDS):
            raise ValueError("gatewaySessionId e gatewayReceivedAtMs devem aparecer juntos e ser validos")
        summary = MetricSummary(
            kind="HDOP", **common, session_id=data["collarSessionId"],
            started_at_ms=data["collarStartedAtMs"], hdop_mean_x100=data["hdopMeanX100"],
            hdop_mean_valid=data["hdopMeanValid"], gateway_session_id=data.get("gatewaySessionId"),
            gateway_received_at_ms=data.get("gatewayReceivedAtMs"),
        )
    elif kind == RADIO_SUMMARY_KIND:
        _check_fields(data, _COMMON_FIELDS | _RADIO_FIELDS)
        summary = MetricSummary(
            kind="RADIO", **common, session_id=data["gatewaySessionId"],
            started_at_ms=data["gatewayStartedAtMs"], rssi_mean_x100=data["rssiMeanX100"],
            snr_mean_x100=data["snrMeanX100"], rssi_mean_valid=data["rssiMeanValid"], snr_mean_valid=data["snrMeanValid"],
        )
    else:
        raise ValueError("kind de resumo desconhecido")
    validate_metric_summary(summary, require_producer=False)
    return summary
