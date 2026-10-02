"""Saída das rotas de rebanho, eventos e Base."""

from __future__ import annotations

from pydantic import BaseModel


class CollarOut(BaseModel):
    """Último estado conhecido de uma coleira.

    Tudo aqui vem da coleira, pela Base. O Central não calcula zona nem
    bateria — só `outdated`, que compara a versão relatada com a ativa.
    """

    collar_id: str
    base_id: str | None = None
    animal_label: str | None = None
    fence_version_reported: int | None = None
    last_zone: str | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    battery_pct: int | None = None
    rssi: int | None = None
    snr: float | None = None
    last_seen: str | None = None
    outdated: bool = False


class TelemetryOut(BaseModel):
    """Uma posição recebida."""

    id: int
    base_id: str
    edge_seq: int
    collar_id: str
    ts: str
    lat: float | None = None
    lon: float | None = None
    zone: str | None = None
    satellites: int | None = None
    hdop: float | None = None
    battery_pct: int | None = None
    fence_version: int | None = None
    rssi: int | None = None
    snr: float | None = None


class EventOut(BaseModel):
    """Um acontecimento relatado pela coleira."""

    id: int
    base_id: str | None = None
    edge_seq: int | None = None
    collar_id: str | None = None
    kind: str
    zone: str | None = None
    detail: str | None = None
    ts: str


class BaseOut(BaseModel):
    """Situação de uma Base. Sem token e sem hash de token."""

    base_id: str
    lat: float | None = None
    lon: float | None = None
    online: bool
    last_heartbeat: str | None = None
    serial_status: str | None = None
    queue_size: int | None = None
    fence_version_reported: int | None = None
    fence_version_active: int | None = None
    outdated: bool = False
