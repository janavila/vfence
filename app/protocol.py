from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

VALID_ZONES = {"SEGURO", "ATENCAO", "CRITICO", "FORA", "GNSS_INVALIDO"}
LEGACY_PATTERN = re.compile(
    r"^Recebido:\s*VFENCE_TESTE_(?P<sequence>\d+)\s*\|\s*"
    r"RSSI:\s*(?P<rssi>-?\d+(?:\.\d+)?)\s*dBm\s*\|\s*"
    r"SNR:\s*(?P<snr>-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE,
)
RECEIVER_PATTERN = re.compile(
    r"^Recebido:\s*(?P<payload>.+?)\s*\|\s*"
    r"RSSI:\s*(?P<rssi>-?\d+(?:\.\d+)?)\s*dBm\s*\|\s*"
    r"SNR:\s*(?P<snr>-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE,
)


class ProtocolError(ValueError):
    """A received line is not a valid VFence packet."""


@dataclass(slots=True)
class ParsedPacket:
    version: str
    collar_id: str
    type: str
    timestamp: datetime
    raw_message: str
    latitude: float | None = None
    longitude: float | None = None
    zone: str | None = None
    satellites: int | None = None
    hdop: float | None = None
    rssi: float | None = None
    snr: float | None = None
    sequence: int | None = None

    def as_dict(self) -> dict:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["timestamp"] = self.timestamp.isoformat()
        return data


def parse_message(line: str | bytes, *, timestamp: datetime | None = None, max_length: int = 512) -> ParsedPacket | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("mensagem não é UTF-8 válido") from exc
    raw = line.strip()
    if not raw:
        return None
    if len(raw) > max_length:
        raise ProtocolError(f"mensagem excede {max_length} caracteres")
    received_at = timestamp or datetime.now(timezone.utc)
    legacy = LEGACY_PATTERN.fullmatch(raw)
    if legacy:
        return ParsedPacket(
            version="LEGACY", collar_id="LEGACY", type="TEST",
            timestamp=received_at, raw_message=raw,
            sequence=int(legacy.group("sequence")), rssi=float(legacy.group("rssi")),
            snr=float(legacy.group("snr")),
        )
    receiver = RECEIVER_PATTERN.fullmatch(raw)
    if receiver and receiver.group("payload").startswith("V1|"):
        packet = parse_message(receiver.group("payload"), timestamp=received_at, max_length=max_length)
        packet.rssi = float(receiver.group("rssi"))
        packet.snr = float(receiver.group("snr"))
        packet.raw_message = raw
        return packet
    fields = raw.split("|")
    if not fields or fields[0] != "V1":
        raise ProtocolError("formato desconhecido")
    if len(fields) != 9:
        raise ProtocolError("pacote V1 POS deve conter 9 campos")
    version, collar_id, packet_type, lat, lon, zone, satellites, hdop, sequence = fields
    collar_id, packet_type, zone = collar_id.strip(), packet_type.strip().upper(), zone.strip().upper()
    if not collar_id or len(collar_id) > 32:
        raise ProtocolError("identificador de coleira inválido")
    if packet_type != "POS":
        raise ProtocolError("tipo V1 não suportado")
    if zone not in VALID_ZONES:
        raise ProtocolError("zona inválida")
    try:
        latitude, longitude = float(lat), float(lon)
        satellites_value, hdop_value, sequence_value = int(satellites), float(hdop), int(sequence)
    except ValueError as exc:
        raise ProtocolError("campo numérico inválido") from exc
    if not -90 <= latitude <= 90:
        raise ProtocolError("latitude fora do intervalo")
    if not -180 <= longitude <= 180:
        raise ProtocolError("longitude fora do intervalo")
    if not math.isfinite(hdop_value):
        raise ProtocolError("HDOP inválido")
    if satellites_value < 0 or hdop_value < 0 or sequence_value < 0:
        raise ProtocolError("valor numérico não pode ser negativo")
    return ParsedPacket(
        version=version, collar_id=collar_id, type=packet_type, timestamp=received_at,
        raw_message=raw, latitude=latitude, longitude=longitude, zone=zone,
        satellites=satellites_value, hdop=hdop_value, sequence=sequence_value,
    )
