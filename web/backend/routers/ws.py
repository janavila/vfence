"""WebSocket `/ws` — tempo real para o navegador (seção 6.2).

Por que WebSocket, e por que ele não basta
------------------------------------------
Depois de clicar em Enviar, o produtor fica olhando a tela esperando
"Confirmada". Pedir para ele recarregar a página seria inaceitável.

Mas o WebSocket é a parte mais frágil da pilha: cai em rede instável e
alguns intermediários o bloqueiam. Por isso o navegador tem plano B
(consulta a cada 5 s, em `frontend/js/realtime.js`) — e as rotas de
leitura comuns continuam existindo e completas. O tempo real é
conveniência; nenhuma informação existe SÓ por aqui.

Quem envia o quê
----------------
`routers/edge.py` é quem dispara as mensagens, ao gravar o que a Base
enviou. Esta rota só mantém a lista de navegadores conectados — o
`RealtimeHub` em `services/realtime.py`.

Mensagens recebidas do navegador são ignoradas de propósito: a
comunicação aqui é de mão única. O `receive_text()` existe só para
detectar quando a conexão caiu.
"""

from __future__ import annotations

import logging

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.clock import utc_now_iso
from backend.permissions import browser_origin_matches, role_for, session_user
from backend.services import edge_service, fence_service

logger = logging.getLogger("realtime")

router = APIRouter()


@router.websocket("/ws")
@router.websocket("/ws/producer")
async def websocket_route(websocket: WebSocket) -> None:
    """Aceita a conexão e mantém o navegador na lista de difusão.

    Logo ao conectar, mandamos um retrato da situação atual. Sem isso, a
    tela ficaria vazia até o próximo acontecimento — que pode levar
    minutos se o rebanho estiver parado.
    """
    app = websocket.app
    hub = app.state.hub
    db = app.state.db
    settings = app.state.settings

    # O middleware de sessão do `main.py` só vê requisições HTTP: o
    # protocolo do WebSocket não passa por ele. Por isso a conferência
    # de login acontece aqui, na própria rota. Sem isso, qualquer pessoa
    # na rede receberia a telemetria do rebanho sem fazer login.
    restricted = websocket.url.path == "/ws/producer"
    user = session_user(websocket)
    if (not user or (not restricted and role_for(settings, user) != "management")
            or not browser_origin_matches(websocket)):
        # 1008 é "policy violation" no protocolo do WebSocket.
        await websocket.close(code=1008, reason="not_authenticated")
        logger.info("websocket refused: no session")
        return

    # Com o banco fora do ar não há retrato para mandar. 1013 é "tente
    # mais tarde": o navegador segue na consulta a cada 5 s, que recebe
    # 503 até o banco voltar (ver `exigir_banco` no main.py).
    if not getattr(app.state, "db_ready", True):
        await websocket.close(code=1013, reason="database_unavailable")
        return

    if restricted:
        hello = {"type": "invalidate"}
    else:
        active, bases = await asyncio.to_thread(lambda: (fence_service.active_version(db), _all_bases(db)))
        hello = {"type": "hello", "data": {
            "server_time": utc_now_iso(), "active_fence_version": active,
            "bases": [
                {"base_id": base["base_id"], "online": edge_service.base_is_online(base, settings),
                 "last_heartbeat": base["last_heartbeat"]} for base in bases
            ],
        }}
    if not await hub.connect(websocket, hello, invalidation_only=restricted):
        return
    try:
        while session_user(websocket):
            try:
                await asyncio.wait_for(websocket.receive_text(), 5)
            except TimeoutError:
                pass
        await websocket.close(code=1008, reason="session_expired")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await hub.disconnect(websocket)


def _all_bases(db) -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM bases ORDER BY base_id")]
