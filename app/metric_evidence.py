from __future__ import annotations

import re
from dataclasses import dataclass


EVIDENCE_SCHEMA = "vfence.gateway.evidence.v1"
EVIDENCE_KIND = "metric_evidence"
_IDENTITY = re.compile(r"[0-9a-f]{16}")
_FIELDS = {
    "schema", "kind", "collarId", "gatewaySessionId", "collarSessionId", "windowId", "startedAtMs",
    "durationMs", "profileId", "txFirst", "txLast", "receivedCount", "pdrX100",
    "radioCount", "rssiX100", "snrX100", "hdopCount", "hdopX100",
}


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    collar_id: int
    gateway_session_id: str
    collar_session_id: str
    window_id: str
    started_at_ms: int
    profile_id: int
    tx_first: int
    tx_last: int
    received: int
    pdr: int | None
    radio_count: int
    rssi: int | None
    snr: int | None
    hdop_count: int
    hdop: int | None

    @property
    def transmitted(self):
        return (self.tx_last - self.tx_first) & 0xFFFFFFFF

    def project(self, gateway_id, installation_id, persisted_at):
        common = dict(collar_id=self.collar_id, window_id=self.window_id, started_at_ms=self.started_at_ms,
                      duration_ms=60000, persisted_at=persisted_at, gateway_id=gateway_id,
                      installation_id=installation_id, gateway_session_id=self.gateway_session_id,
                      collar_session_id=self.collar_session_id, profile_id=self.profile_id)
        pdr = dict(common, source="proven", tx_first=self.tx_first, tx_last=self.tx_last,
                   transmitted_count=self.transmitted, lost_count=self.transmitted-self.received,
                   received_count=self.received, pdr_x100=self.pdr, valid=self.pdr is not None,
                   estimated_tx_count=self.transmitted, estimated_lost_count=self.transmitted-self.received,
                   reason="ok" if self.transmitted else "no_samples", tx_proven=1)
        metric = dict(common, hdop_mean_x100=None, hdop_mean_valid=None, rssi_mean_x100=None,
                      snr_mean_x100=None, rssi_mean_valid=None, snr_mean_valid=None,
                      gateway_received_at_ms=None, timing_proven=1)
        radio = dict(metric, kind="RADIO", producer_id=gateway_id, session_id=self.gateway_session_id,
                     sample_count=self.radio_count, rssi_mean_x100=self.rssi, snr_mean_x100=self.snr,
                     rssi_mean_valid=bool(self.radio_count), snr_mean_valid=bool(self.radio_count))
        hdop = dict(metric, kind="HDOP", producer_id=installation_id, session_id=self.collar_session_id,
                    sample_count=self.hdop_count, hdop_mean_x100=self.hdop, hdop_mean_valid=bool(self.hdop_count))
        return {"pdr": pdr, "radio": radio, "hdop": hdop}


def parse_evidence(data: dict) -> MetricEvidence:
    if data.keys() != _FIELDS or data["schema"] != EVIDENCE_SCHEMA or data["kind"] != EVIDENCE_KIND:
        raise ValueError("contrato de evidencia invalido")

    for key in ("gatewaySessionId", "collarSessionId", "windowId"):
        value = data[key]
        if not isinstance(value, str) or not _IDENTITY.fullmatch(value) or int(value, 16) == 0:
            raise ValueError(f"{key} invalido")

    def integer(key, lo, hi):
        value = data[key]
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"{key} invalido")
        return value

    def mean(key, count, lo, hi):
        if count:
            return integer(key, lo, hi)
        if data[key] is not None:
            raise ValueError(f"{key} incoerente")
        return None

    integer("durationMs", 60000, 60000)
    collar = integer("collarId", 1, 254)
    start = integer("startedAtMs", 0, 0xFFFFFFFF)
    profile = integer("profileId", 0, 3)
    first, last = (integer(key, 0, 0xFFFFFFFF) for key in ("txFirst", "txLast"))
    total = (last - first) & 0xFFFFFFFF
    if total > 512:
        raise ValueError("intervalo de transmissoes fora do limite")
    received = integer("receivedCount", 0, total)
    pdr = integer("pdrX100", 0, 10000) if total else data["pdrX100"]
    if pdr != ((10000 * received + total // 2) // total if total else None):
        raise ValueError("PDR incompatível com evidencia")
    radio = integer("radioCount", 0, received)
    hdop = integer("hdopCount", 0, 60)
    return MetricEvidence(collar, data["gatewaySessionId"], data["collarSessionId"], data["windowId"], start,
                          profile, first, last, received, pdr, radio, mean("rssiX100", radio, -25500, 0),
                          mean("snrX100", radio, -32768, 32767), hdop, mean("hdopX100", hdop, 0, 65534))
