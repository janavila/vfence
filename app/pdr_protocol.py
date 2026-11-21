from __future__ import annotations

import re
from dataclasses import dataclass


PDR_SCHEMA = "vfence.gateway.pdr.v1"
PDR_COMMIT_SCHEMA = "vfence.gateway.pdr.v2"
PDR_KIND = "pdr_window_summary"
_HEX_ID = re.compile(r"[0-9a-f]{16}")
_REASONS = {"ok", "no_samples", "one_sample", "session_changed", "profile_changed", "discontinuity"}
_FIELDS = {
    "schema", "kind", "collarId", "gatewaySessionId", "windowId", "collarSessionId",
    "startedAtMs", "durationMs", "profileId", "receivedCount", "estimatedTxCount",
    "estimatedLostCount", "pdrX100", "valid", "reason",
}


@dataclass(frozen=True, slots=True)
class PdrSummary:
    collar_id: int
    gateway_session_id: str
    window_id: str
    collar_session_id: str | None
    started_at_ms: int
    duration_ms: int
    profile_id: int | None
    received_count: int
    estimated_tx_count: int | None
    estimated_lost_count: int | None
    pdr_x100: int | None
    valid: bool
    reason: str


def parse_pdr_summary(data: dict) -> PdrSummary:
    if data.keys() != _FIELDS or data["schema"] not in (PDR_SCHEMA, PDR_COMMIT_SCHEMA) or data["kind"] != PDR_KIND:
        raise ValueError("contrato PDR invalido")

    for key in ("gatewaySessionId", "windowId", "collarSessionId"):
        value = data[key]
        if key == "collarSessionId" and value is None:
            continue
        if not isinstance(value, str) or not _HEX_ID.fullmatch(value):
            raise ValueError(f"{key} invalido")
    if data["windowId"] == "0000000000000000":
        raise ValueError("windowId invalido")

    def integer(key: str, minimum: int, maximum: int) -> int:
        value = data[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} invalido")
        return value

    collar_id = integer("collarId", 1, 254)
    started_at_ms = integer("startedAtMs", 0, 0xFFFFFFFF)
    duration_ms = integer("durationMs", 60000, 60000)
    received = integer("receivedCount", 0, 64)
    profile = None if data["profileId"] is None else integer("profileId", 0, 3)
    valid, reason = data["valid"], data["reason"]
    if type(valid) is not bool or not isinstance(reason, str) or reason not in _REASONS or valid != (reason == "ok"):
        raise ValueError("validade PDR incoerente")
    expected, lost, pdr = data["estimatedTxCount"], data["estimatedLostCount"], data["pdrX100"]
    if valid:
        expected = integer("estimatedTxCount", 2, 64)
        lost = integer("estimatedLostCount", 0, 62)
        pdr = integer("pdrX100", 1, 10000)
        if (not 2 <= received <= expected or profile is None or data["collarSessionId"] is None
                or lost != expected - received or pdr != (10000 * received + expected // 2) // expected):
            raise ValueError("calculo PDR incoerente")
    elif (any(value is not None for value in (expected, lost, pdr))
          or reason == "no_samples" and (received != 0 or data["collarSessionId"] is not None)
          or reason == "one_sample" and (received != 1 or profile is None or data["collarSessionId"] is None)
          or reason == "session_changed" and data["collarSessionId"] is not None
          or reason == "profile_changed" and profile is not None):
        raise ValueError("janela PDR invalida com campos incoerentes")
    return PdrSummary(collar_id, data["gatewaySessionId"], data["windowId"], data["collarSessionId"],
                      started_at_ms, duration_ms, profile, received, expected, lost, pdr, valid, reason)
