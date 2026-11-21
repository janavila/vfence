"""Leitura do rebanho, dos eventos e da Base.

Reúne três rotas da seção 6 que são todas "mostre o que você sabe":

| Rota                                       | Tela que usa       |
|--------------------------------------------|--------------------|
| GET /api/collars                           | Rebanho, Início    |
| GET /api/collars/{collar_id}/telemetry     | Rebanho (detalhe)  |
| GET /api/events?limit=                     | Eventos, Início    |
| GET /api/bases                             | cabeçalho, Início  |

Estão juntas em um arquivo porque são leituras simples do mesmo
assunto — o estado reportado pelo campo. A seção 4 da especificação
prevê `collars.py`, `events.py` e `bases.py` separados; juntamos para
não ter três arquivos de trinta linhas, e o nome `monitoring` deixa
claro o que reúne.

O Central não decide nada aqui
------------------------------
Zona, bateria, RSSI e SNR vêm da coleira pela Base. Estas rotas apenas
devolvem o que está gravado, mais dois campos CALCULADOS que a tela
precisa e que não valeria gravar:

- `online` da Base: comparação do último heartbeat com
  `BASE_OFFLINE_APOS_S`. Gravar seria inventar um valor que envelhece.
- `outdated` da coleira: a versão de cerca que ela relatou é diferente
  da ativa. É o "estado reportado × estado desejado" da seção 3.2 do
  planejamento, que aparece na tela como "desatualizada".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from backend.schemas.monitoring import AnimalLabelIn, BaseOut, CollarOut, EventOut, TelemetryOut
from backend.services import edge_service, fence_service

logger = logging.getLogger("monitoring")

router = APIRouter(prefix="/api", tags=["rebanho"])


# ---------------------------------------------------------------------------
# Coleiras
# ---------------------------------------------------------------------------


def _collars(db, active_version: int | None) -> list[dict]:
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT collars.*,(SELECT MAX(fence_version) FROM deliveries "
            "WHERE deliveries.collar_id=collars.collar_id AND status='confirmed') AS confirmed_fence_version "
            "FROM collars ORDER BY collar_id",
        )]
    now = datetime.now(timezone.utc)
    for row in rows:
        stamp = row["last_position_at"]
        age = None
        if stamp:
            try:
                moment = datetime.fromisoformat(stamp)
                age = max(0, int((now - moment).total_seconds())) if moment.tzinfo else None
            except ValueError:
                pass
        row["position_age_s"] = age
        row["position_stale"] = age is None or age > 120 or row["gnss_status"] == "invalid"
        row["outdated"] = active_version is not None and row["fence_version_reported"] != active_version
    return rows


@router.get("/collars", response_model=list[CollarOut], summary="Coleiras e último estado")
async def collars_route(request: Request) -> list[dict]:
    db = request.app.state.db
    active = await asyncio.to_thread(fence_service.active_version, db)
    return await asyncio.to_thread(_collars, db, active)


@router.patch("/collars/{collar_id}", response_model=CollarOut)
async def change_animal_label(collar_id: str, body: AnimalLabelIn, request: Request) -> dict:
    db = request.app.state.db
    label = body.animal_label.strip() if body.animal_label else None
    if label and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in label):
        raise HTTPException(422, "invalid_animal_label")

    def update():
        with db.connect() as conn:
            cursor = conn.execute("UPDATE collars SET animal_label=? WHERE collar_id=?", (label or None, collar_id))
            if cursor.rowcount == 0:
                return None
            return next(row for row in _collars(db, fence_service.active_version(db)) if row["collar_id"] == collar_id)

    updated = await asyncio.to_thread(update)
    if updated is None:
        raise HTTPException(404, "collar_not_found")
    return updated


def _history_filter(collar_id, start, end, before_id):
    for bound in (start, end):
        if bound is not None and bound.tzinfo is None:
            raise HTTPException(422, "history_date_requires_timezone")
    if start is not None and end is not None and start >= end:
        raise HTTPException(422, "invalid_history_period")
    filters, args = [], []
    for condition, value in (
        ("collar_id=?", collar_id), ("id<?", before_id),
        ("substr(ts,1,19)>=?", start.astimezone(timezone.utc).isoformat()[:19] if start else None),
        ("substr(ts,1,19)<?", end.astimezone(timezone.utc).isoformat()[:19] if end else None),
    ):
        if value is not None:
            filters.append(condition)
            args.append(value)
    return (" WHERE " + " AND ".join(filters) if filters else ""), args


@router.get(
    "/collars/{collar_id}/telemetry",
    response_model=list[TelemetryOut],
    summary="Telemetria recente de uma coleira",
)
async def telemetry_route(
    collar_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    start: datetime | None = None,
    end: datetime | None = None,
    before_id: int | None = Query(None, ge=1),
) -> list[dict]:
    """Mais recente primeiro.

    Ordenamos por `id`, não por `ts`: `id` é a ordem real de chegada e
    não empata quando dois pacotes têm o mesmo segundo — e o relógio da
    coleira pode até andar para trás, já que o Raspberry não tem relógio
    de bateria.
    """
    db = request.app.state.db
    where, parameters = _history_filter(collar_id, start, end, before_id)

    def consultar() -> list[dict] | None:
        with db.connect() as conn:
            existe = conn.execute(
                "SELECT 1 FROM collars WHERE collar_id = ?", (collar_id,)
            ).fetchone()
            if existe is None:
                return None
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM telemetry" + where + " ORDER BY id DESC LIMIT ?",
                    (*parameters, limit),
                )
            ]

    resultado = await asyncio.to_thread(consultar)
    if resultado is None:
        raise HTTPException(status_code=404, detail="collar_not_found")
    return resultado


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------


@router.get("/events", response_model=list[EventOut], summary="Eventos recentes")
async def events_route(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    collar_id: str | None = Query(None, description="filtra por coleira"),
    start: datetime | None = None,
    end: datetime | None = None,
    before_id: int | None = Query(None, ge=1),
) -> list[dict]:
    """Mais recente primeiro, com filtro opcional por coleira.

    O filtro atende a seção 11.4 ("linha do tempo com filtro por
    coleira"). Ele é um parâmetro OPCIONAL, então a rota continua
    atendendo o contrato da seção 6 sem ele.
    """
    db = request.app.state.db
    where, parameters = _history_filter(collar_id, start, end, before_id)

    def consultar() -> list[dict]:
        with db.connect() as conn:
            linhas = conn.execute(
                "SELECT * FROM events" + where + " ORDER BY id DESC LIMIT ?", (*parameters, limit),
            )
            return [dict(linha) for linha in linhas]

    return await asyncio.to_thread(consultar)


# ---------------------------------------------------------------------------
# Bases
# ---------------------------------------------------------------------------


@router.get("/bases", response_model=list[BaseOut], summary="Bases e último contato")
async def bases_route(request: Request) -> list[dict]:
    """Nunca devolve o token nem o hash dele.

    A tela só precisa saber se a Base está online e quando foi o último
    contato. Existe um teste que falha se alguém acrescentar o hash por
    descuido.
    """
    db = request.app.state.db
    settings = request.app.state.settings
    active = await asyncio.to_thread(fence_service.active_version, db)

    def consultar() -> list[dict]:
        with db.connect() as conn:
            linhas = [dict(linha) for linha in conn.execute("SELECT * FROM bases ORDER BY base_id")]
        return [
            {
                "base_id": linha["base_id"],
                "lat": linha["lat"],
                "lon": linha["lon"],
                "online": edge_service.base_is_online(linha, settings),
                "last_heartbeat": linha["last_heartbeat"],
                "serial_status": linha["serial_status"],
                "queue_size": linha["queue_size"],
                "fence_version_reported": linha["fence_version_reported"],
                "fence_version_active": active,
                "outdated": (
                    active is not None and linha["fence_version_reported"] != active
                ),
            }
            for linha in linhas
        ]

    return await asyncio.to_thread(consultar)
