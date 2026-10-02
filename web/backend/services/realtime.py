"""Envio de mensagens aos navegadores conectados (WebSocket).

De onde vem este código
-----------------------
É o `RealtimeHub` do VFence Monitor (`app/realtime.py`), reaproveitado
quase literal. Mesma estrutura: um conjunto de conexões, uma trava do
`asyncio` para alterá-lo, e um envio que descarta as conexões mortas.

Por que descartar as mortas no envio
------------------------------------
Quando alguém fecha a aba, o servidor não é avisado na hora. A conexão
continua no conjunto e só falha na próxima tentativa de envio. Se não
removêssemos ali, o conjunto cresceria para sempre e cada mensagem
tentaria falar com abas que não existem mais.

Tipos de mensagem (seção 6.2 da especificação)
----------------------------------------------
    {"type": "delivery",  "data": {...}}   mudou o estado de uma entrega
    {"type": "telemetry", "data": {...}}   chegou posição de uma coleira
    {"type": "event",     "data": {...}}   a coleira relatou um evento
    {"type": "base",      "data": {...}}   a Base ficou online ou offline

Onde se conecta
---------------
`routers/edge.py` chama `broadcast()` depois de gravar cada lote da Base;
`routers/ws.py` (fase F6) aceita as conexões do navegador. O hub fica em
`app.state.hub`, criado no `main.py`.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger("realtime")


class RealtimeHub:
    """Conjunto de navegadores conectados, com envio em difusão."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        """Quantos navegadores estão ouvindo. Usado no diagnóstico."""
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("browser connected (%d listening)", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("browser disconnected (%d listening)", len(self._connections))

    async def broadcast(self, message_type: str, data: dict) -> None:
        """Manda uma mensagem para todos os navegadores conectados.

        Nunca levanta exceção: uma aba fechada no meio do envio não pode
        derrubar a gravação do lote que a Base acabou de enviar.
        """
        message = {"type": message_type, "data": data}
        async with self._lock:
            connections = list(self._connections)
        if not connections:
            return

        dead: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections.discard(websocket)
            logger.info("dropped %d closed connection(s)", len(dead))

    async def broadcast_many(self, messages: list[tuple[str, dict]]) -> None:
        """Manda várias mensagens, na ordem. Usado ao gravar um lote."""
        for message_type, data in messages:
            await self.broadcast(message_type, data)
