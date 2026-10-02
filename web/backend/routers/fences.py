"""Rotas de cerca: `/api/fences/*` (seção 6 da especificação).

| Método e rota                             | O que faz                       |
|-------------------------------------------|---------------------------------|
| POST   /api/fences/validate               | valida sem salvar               |
| POST   /api/fences                        | cria versão nova e ativa        |
| GET    /api/fences/active                 | a cerca que está valendo        |
| GET    /api/fences                        | histórico, mais recente antes   |
| GET    /api/fences/{version}              | uma versão                      |
| POST   /api/fences/{version}/reactivate   | copia os pontos para versão nova|
| GET    /api/fences/{version}/deliveries   | entrega por coleira             |
| GET    /api/fences/{version}/log          | arquivo de log (.txt)           |
| GET    /api/fences/{version}/geojson      | exportação                      |

A ordem de declaração importa
-----------------------------
`/api/fences/active` é declarada ANTES de `/api/fences/{version}`. O
FastAPI casa as rotas na ordem em que foram declaradas; se a rota com
parâmetro viesse primeiro, ela capturaria o texto "active" e tentaria
convertê-lo em número, respondendo um 422 confuso.

Onde se conecta
---------------
Só conversa com `services/fence_service.py`, `services/delivery.py` e
`services/fence_log.py`. Toda decisão sobre o que é uma cerca válida
está nos serviços; aqui ficam apenas a tradução HTTP e os códigos de
resposta.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.config import Settings
from backend.db import Database
from backend.errors import FenceInvalid, WarningsNotAccepted
from backend.schemas.fences import (
    DeliveryOut,
    FenceIn,
    FenceOut,
    FenceSummaryOut,
    ValidationOut,
)
from backend.services import delivery, fence_log, fence_service
from backend.services.geometry import GeoPoint

logger = logging.getLogger("fences")

router = APIRouter(prefix="/api/fences", tags=["cercas"])


def _context(request: Request) -> tuple[Database, Settings]:
    """Pega o banco e a configuração guardados em `app.state`.

    Usar `app.state` em vez de variável global é o que permite dois apps
    de teste rodarem no mesmo processo sem um ver o banco do outro.
    """
    return request.app.state.db, request.app.state.settings


def _as_geopoints(data: FenceIn) -> list[GeoPoint]:
    return [GeoPoint(point.lat, point.lon) for point in data.points]


# ---------------------------------------------------------------------------
# Validar sem salvar
# ---------------------------------------------------------------------------


@router.post(
    "/validate",
    response_model=ValidationOut,
    summary="Valida uma cerca sem salvar",
)
async def validate_fence_route(data: FenceIn, request: Request) -> dict:
    """Devolve erros, avisos, área, perímetro e sentido.

    Nunca responde erro HTTP por cerca inválida: a resposta 200 com
    `valid: false` é o resultado esperado. Quem chama é o editor, que
    usa isto para mostrar os problemas enquanto o produtor desenha — e
    uma cerca pela metade é a situação NORMAL nesse momento, não um erro.
    """
    db, settings = _context(request)
    result = await asyncio.to_thread(
        fence_service.validate,
        _as_geopoints(data),
        data.margin_attention_m,
        data.margin_critical_m,
        settings,
    )
    return result.as_dict()


# ---------------------------------------------------------------------------
# Criar
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=FenceOut,
    status_code=201,
    summary="Cria uma versão nova e a torna ativa",
)
async def create_fence_route(data: FenceIn, request: Request) -> dict:
    """Valida, grava a versão nova, ativa, e cria as entregas pendentes.

    Três respostas possíveis:

    - **201** com a cerca criada;
    - **422 `fence_invalid`** se houver qualquer erro de validação;
    - **409 `warnings_not_accepted`** se houver avisos e o corpo não
      trouxer `accept_warnings: true`. A cerca não está errada: falta o
      produtor marcar "Estou ciente dos avisos" na revisão.
    """
    db, settings = _context(request)
    points = _as_geopoints(data)

    result = await asyncio.to_thread(
        fence_service.validate, points, data.margin_attention_m, data.margin_critical_m, settings
    )
    if not result.valid:
        logger.info(
            "fence creation refused: %s", ", ".join(v.rule for v in result.errors)
        )
        raise FenceInvalid(result.as_dict()["violations"])

    if result.warnings and not data.accept_warnings:
        logger.info(
            "fence creation on hold: warnings not accepted (%s)",
            ", ".join(v.rule for v in result.warnings),
        )
        raise WarningsNotAccepted(result.as_dict()["violations"])

    return await asyncio.to_thread(
        fence_service.create_fence,
        db, settings,
        name=data.name,
        margin_attention_m=data.margin_attention_m,
        margin_critical_m=data.margin_critical_m,
        points=points,
        result=result,
    )


# ---------------------------------------------------------------------------
# Consultar
# ---------------------------------------------------------------------------


@router.get("/active", response_model=FenceOut, summary="A cerca que está valendo")
async def get_active_route(request: Request) -> dict:
    db, _ = _context(request)
    fence = await asyncio.to_thread(fence_service.get_active, db)
    if fence is None:
        raise HTTPException(status_code=404, detail="no_active_fence")
    return fence


@router.get("", response_model=list[FenceSummaryOut], summary="Histórico de cercas")
async def list_fences_route(request: Request) -> list[dict]:
    db, _ = _context(request)
    return await asyncio.to_thread(fence_service.list_summaries, db)


@router.get("/{version}", response_model=FenceOut, summary="Uma versão de cerca")
async def get_fence_route(version: int, request: Request) -> dict:
    db, _ = _context(request)
    try:
        return await asyncio.to_thread(fence_service.get_by_version, db, version)
    except fence_service.FenceNotFound:
        raise HTTPException(status_code=404, detail="fence_not_found") from None


# ---------------------------------------------------------------------------
# Reativar
# ---------------------------------------------------------------------------


@router.post(
    "/{version}/reactivate",
    response_model=FenceOut,
    status_code=201,
    summary="Copia os pontos de uma versão para uma versão nova e ativa",
)
async def reactivate_route(version: int, request: Request) -> dict:
    """Reativar NÃO reabre a versão antiga: cria uma nova com os mesmos
    pontos. Assim "maior versão = mais recente" continua valendo e o log
    da versão antiga segue verdadeiro.

    Pode responder 422 se a cerca antiga não passar mais na validação —
    por exemplo, se a Base mudou de lugar no `.env` e os pontos agora
    estão fora do raio da regra VAL-03.
    """
    db, settings = _context(request)
    try:
        return await asyncio.to_thread(fence_service.reactivate, db, settings, version)
    except fence_service.FenceNotFound:
        raise HTTPException(status_code=404, detail="fence_not_found") from None
    except fence_service.FenceRefused as refused:
        raise FenceInvalid(refused.result.as_dict()["violations"]) from None


# ---------------------------------------------------------------------------
# Entrega, log e exportação
# ---------------------------------------------------------------------------


@router.get(
    "/{version}/deliveries",
    response_model=list[DeliveryOut],
    summary="Situação da entrega por coleira",
)
async def deliveries_route(version: int, request: Request) -> list[dict]:
    db, _ = _context(request)
    await _ensure_exists(db, version)
    return await asyncio.to_thread(delivery.list_for_fence, db, version)


@router.get(
    "/{version}/log",
    response_class=PlainTextResponse,
    summary="Arquivo de log da cerca (download)",
)
async def log_route(version: int, request: Request) -> Response:
    """Devolve o `.txt` montado a partir da tabela `fence_log_lines`.

    O cabeçalho `Content-Disposition: attachment` faz o navegador baixar
    em vez de mostrar, porque o arquivo é anexo de relatório.
    """
    db, _ = _context(request)
    fence = await _ensure_exists(db, version)
    deliveries = await asyncio.to_thread(delivery.list_for_fence, db, version)
    text = await asyncio.to_thread(fence_log.render, db, fence, deliveries)
    return PlainTextResponse(
        text,
        headers={
            "Content-Disposition": f'attachment; filename="vfence_cerca_{version}.txt"'
        },
    )


@router.get("/{version}/geojson", summary="Exportação em GeoJSON")
async def geojson_route(version: int, request: Request) -> Response:
    """GeoJSON conforme a RFC 7946, para abrir no geojson.io ou no QGIS."""
    db, _ = _context(request)
    fence = await _ensure_exists(db, version)
    content = fence_log.to_geojson(fence)
    return JSONResponse(
        content=content,
        media_type="application/geo+json",
        headers={
            "Content-Disposition": f'attachment; filename="vfence_cerca_{version}.geojson"'
        },
    )


async def _ensure_exists(db: Database, version: int) -> dict:
    """Busca a cerca ou responde 404, para as rotas filhas não repetirem isso."""
    try:
        return await asyncio.to_thread(fence_service.get_by_version, db, version)
    except fence_service.FenceNotFound:
        raise HTTPException(status_code=404, detail="fence_not_found") from None
