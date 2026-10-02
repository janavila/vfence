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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.clock import utc_now_iso
from backend.services import edge_service, fence_service

logger = logging.getLogger("realtime")

router = APIRouter()


@router.websocket("/ws")
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
    if settings.auth_enabled and app.state.sessao_valida(websocket) is None:
        # 1008 é "policy violation" no protocolo do WebSocket.
        await websocket.close(code=1008, reason="not_authenticated")
        logger.info("websocket refused: no session")
        return

    await hub.connect(websocket)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "data": {
                    "server_time": utc_now_iso(),
                    "active_fence_version": fence_service.active_version(db),
                    "bases": [
                        {
                            "base_id": base["base_id"],
                            "online": edge_service.base_is_online(base, settings),
                            "last_heartbeat": base["last_heartbeat"],
                        }
                        for base in _all_bases(db)
                    ],
                },
            }
        )
        while True:
            # Só serve para perceber a desconexão.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
    except Exception as erro:  # noqa: BLE001
        logger.info("websocket closed unexpectedly (%s)", erro)
        await hub.disconnect(websocket)


def _all_bases(db) -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM bases ORDER BY base_id")]
