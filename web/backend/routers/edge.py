"""Rotas usadas pela Base: `/api/edge/*` (contrato I2, seção 9).

Quem chama quem, e por quê
--------------------------
O Central NUNCA chama a Base. É sempre a Base que chama o Central.

O motivo é prático: no campo, o Raspberry fica atrás de um roteador ou
modem 4G, quase sempre com CGNAT. Ele acessa a internet, mas não pode
ser acessado de fora. Se o Central precisasse iniciar a conexão, nada
funcionaria fora do laboratório (seção 3.1 do planejamento).

A consequência boa: levar o Central da rede local para a nuvem exige
trocar uma linha (`CENTRAL_URL`) na configuração da Base.

As três rotas
-------------
| Rota                               | Para que serve                        |
|------------------------------------|---------------------------------------|
| POST /api/edge/heartbeat           | "estou vivo, tenho a cerca N"         |
| GET  /api/edge/fences/{version}    | baixar a cerca na forma canônica      |
| POST /api/edge/batch               | entregar telemetria, eventos, status  |

Autenticação
------------
Todas exigem `Authorization: Bearer <BASE_TOKEN>`. O Central guarda só o
hash do token (`bases.token_hash`), então a conferência recalcula o hash
do que chegou e compara.

Como o `GET` não tem corpo, a Base é identificada PELO TOKEN. Com uma
Base por propriedade (premissa P3) isso é direto. Se um dia houver
várias, a busca por token precisará de um índice — está anotado no
`_base_from_token()`.

Onde se conecta
---------------
Usa `services/edge_service.py` para gravar, `services/delivery.py` (por
meio dele) para os estados de entrega, e `services/realtime.py` para
avisar os navegadores abertos.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from backend.config import Settings
from backend.db import Database
from backend.schemas.edge import (
    BatchIn,
    BatchOut,
    FenceForBaseOut,
    HeartbeatIn,
    HeartbeatOut,
)
from backend.security import verify_token
from backend.services import delivery, edge_service, fence_service
from backend.services.canonical import to_cm

logger = logging.getLogger("edge")

router = APIRouter(prefix="/api/edge", tags=["base"])


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------


def _token_from_header(authorization: str | None) -> str:
    """Extrai o token de `Authorization: Bearer <token>`.

    Responde 401 em qualquer formato inesperado, sem dizer o que estava
    errado: detalhar ajudaria quem está tentando adivinhar.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="missing_token")
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="invalid_token")
    return parts[1].strip()


def _base_from_token(db: Database, token: str) -> dict:
    """Descobre qual Base é dona do token.

    Percorre as Bases cadastradas e compara o hash. Com uma Base
    (premissa P3) é uma linha; se o projeto crescer para várias
    propriedades, vale indexar `token_hash` e consultar direto por ele.

    A comparação usa `secrets.compare_digest` dentro de `verify_token`,
    e não `==`, para o tempo de resposta não revelar quantos caracteres
    do segredo o atacante acertou.
    """
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM bases").fetchall()
    for row in rows:
        if verify_token(token, row["token_hash"]):
            return dict(row)
    raise HTTPException(status_code=401, detail="invalid_token")


def _authenticate(request: Request, authorization: str | None) -> tuple[Database, Settings, dict]:
    db: Database = request.app.state.db
    settings: Settings = request.app.state.settings
    base = _base_from_token(db, _token_from_header(authorization))
    return db, settings, base


def _check_body_matches_token(base: dict, base_id_in_body: str) -> None:
    """O `base_id` do corpo tem de ser o mesmo que o do token.

    Decisão da fase F0. Com uma Base só isso nunca acontece, mas fecha de
    graça a porta de uma Base usar o token de outra. 403 e não 401: o
    token é válido, o que não bate é a identidade declarada.
    """
    if base_id_in_body.upper() != base["base_id"].upper():
        logger.warning(
            "base=%s token used while declaring base_id=%s: refused",
            base["base_id"], base_id_in_body,
        )
        raise HTTPException(status_code=403, detail="base_id_mismatch")


# ---------------------------------------------------------------------------
# 9.1 Heartbeat
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatOut, summary="Sinal periódico da Base")
async def heartbeat_route(
    data: HeartbeatIn,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Guarda o estado reportado e devolve a versão de cerca desejada.

    É o coração do sistema: a Base chama isto a cada
    `SYNC_INTERVAL_SECONDS` e descobre, pela resposta, se precisa baixar
    uma cerca nova. Nenhuma outra coisa precisa acontecer para a cerca
    chegar ao campo.
    """
    db, _, base = _authenticate(request, authorization)
    _check_body_matches_token(base, data.base_id)

    result = await asyncio.to_thread(
        edge_service.apply_heartbeat,
        db,
        base_id=base["base_id"],
        fence_version=data.fence_version,
        serial=data.serial,
        queue_size=data.queue_size,
    )

    hub = request.app.state.hub
    await hub.broadcast(
        "base",
        {
            "base_id": base["base_id"],
            "online": True,
            "last_heartbeat": result["server_time"],
            "serial_status": data.serial,
            "queue_size": data.queue_size,
            "fence_version_reported": data.fence_version,
        },
    )
    return result


# ---------------------------------------------------------------------------
# 9.2 Download da cerca
# ---------------------------------------------------------------------------


@router.get(
    "/fences/{version}",
    response_model=FenceForBaseOut,
    summary="Baixa uma cerca na forma canônica",
)
async def fence_for_base_route(
    version: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Entrega a cerca em inteiros, e marca as entregas como `at_base`.

    O efeito colateral é de propósito (seção 9.2): o download É a prova
    de que a cerca chegou à propriedade. Sem isso, o produtor não teria
    como distinguir "a Base nem sabe da cerca nova" de "a Base sabe mas
    o rádio não entregou".
    """
    db, _, base = _authenticate(request, authorization)

    try:
        fence = await asyncio.to_thread(fence_service.get_by_version, db, version)
    except fence_service.FenceNotFound:
        raise HTTPException(status_code=404, detail="fence_not_found") from None

    changed = await asyncio.to_thread(
        delivery.mark_at_base, db, version, base["base_id"]
    )
    if changed:
        hub = request.app.state.hub
        for row in await asyncio.to_thread(delivery.list_for_fence, db, version):
            await hub.broadcast("delivery", row)

    return {
        "version": fence["version"],
        "margin_attention_cm": to_cm(fence["margin_attention_m"]),
        "margin_critical_cm": to_cm(fence["margin_critical_m"]),
        "points_e6": [
            [int(round(p["lat"] * 1_000_000)), int(round(p["lon"] * 1_000_000))]
            for p in fence["points"]
        ],
        "crc32": fence["crc32"],
    }


# ---------------------------------------------------------------------------
# 9.3 Lote
# ---------------------------------------------------------------------------


@router.post("/batch", response_model=BatchOut, summary="Recebe a fila local da Base")
async def batch_route(
    data: BatchIn,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Grava telemetria, eventos e status de entrega, sem duplicar.

    A Base pode reenviar o mesmo lote à vontade: itens já conhecidos são
    ignorados em silêncio, pela chave `(base_id, edge_seq)`. A resposta
    diz até onde ela pode limpar a fila local.
    """
    db, settings, base = _authenticate(request, authorization)
    _check_body_matches_token(base, data.base_id)

    acked, messages = await asyncio.to_thread(
        edge_service.process_batch,
        db,
        settings,
        base["base_id"],
        [item.model_dump() for item in data.items],
    )

    await request.app.state.hub.broadcast_many(messages)
    return {"acked_up_to": acked}
