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
from contextlib import suppress
from dataclasses import dataclass

from fastapi import WebSocket

logger = logging.getLogger("realtime")

_MAX_CLIENTS = 32
_QUEUE_SIZE = 32
_IO_TIMEOUT = 1.0


@dataclass(slots=True)
class Client:
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    close_code: int | None = None
    invalidation_only: bool = False


class RealtimeHub:
    """Conjunto de navegadores conectados, com envio em difusão."""

    def __init__(self) -> None:
        self._connections: dict[WebSocket, Client] = {}
        self._closed = False

    @property
    def count(self) -> int:
        """Quantos navegadores estão ouvindo. Usado no diagnóstico."""
        return len(self._connections)

    async def connect(self, websocket: WebSocket, hello: dict, *, invalidation_only: bool = False) -> bool:
        try:
            await asyncio.wait_for(websocket.accept(), _IO_TIMEOUT)
        except (RuntimeError, OSError, TimeoutError):
            return False
        if self._closed or self.count >= _MAX_CLIENTS:
            await self._close_socket(websocket, 1013)
            return False
        client = Client(asyncio.Queue(maxsize=_QUEUE_SIZE), invalidation_only=invalidation_only)
        client.queue.put_nowait(hello)
        self._connections[websocket] = client
        client.task = asyncio.create_task(self._write(websocket, client))
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        client = self._connections.pop(websocket, None)
        if client and client.task:
            client.task.cancel()
            with suppress(asyncio.CancelledError):
                await client.task

    async def broadcast(self, message_type: str, data: dict) -> None:
        """Manda uma mensagem para todos os navegadores conectados.

        Nunca levanta exceção: uma aba fechada no meio do envio não pode
        derrubar a gravação do lote que a Base acabou de enviar.
        """
        message = {"type": message_type, "data": data}
        for client in self._connections.values():
            if client.close_code is not None:
                continue
            try:
                if client.invalidation_only:
                    if client.queue.empty():
                        client.queue.put_nowait({"type": "invalidate"})
                else:
                    client.queue.put_nowait(message)
            except asyncio.QueueFull:
                client.close_code = 1013
                logger.warning("websocket backlog full; client reconnect required")

    async def broadcast_many(self, messages: list[tuple[str, dict]]) -> None:
        """Manda várias mensagens, na ordem. Usado ao gravar um lote."""
        for message_type, data in messages:
            await self.broadcast(message_type, data)

    async def _write(self, websocket: WebSocket, client: Client) -> None:
        try:
            while client.close_code is None:
                message = await client.queue.get()
                if message is None:
                    break
                await asyncio.wait_for(websocket.send_json(message), _IO_TIMEOUT)
        except Exception:
            client.close_code = client.close_code or 1013
        finally:
            self._connections.pop(websocket, None)
            if client.close_code is not None:
                await self._close_socket(websocket, client.close_code)

    @staticmethod
    async def _close_socket(websocket: WebSocket, code: int) -> None:
        with suppress(Exception):
            await asyncio.wait_for(websocket.close(code=code), _IO_TIMEOUT)

    async def close(self) -> None:
        self._closed = True
        clients = tuple(self._connections.values())
        for client in clients:
            client.close_code = 1001
            with suppress(asyncio.QueueFull):
                client.queue.put_nowait(None)
        await asyncio.gather(*(client.task for client in clients), return_exceptions=True)
