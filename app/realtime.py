from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect


logger = logging.getLogger("realtime")
_MAX_CLIENTS = 16
_CLIENT_QUEUE_CAPACITY = 32
_CLIENT_IO_TIMEOUT_SECONDS = 1.0
_UPDATE_TYPES = {"telemetry", "gateway", "gateway_event", "central", "fence", "fence_transfer"}


@dataclass(slots=True)
class _Client:
    queue: asyncio.Queue[dict | None]
    done: asyncio.Future[None]
    closing: bool = False
    close_code: int | None = None


class RealtimeHub:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, _Client] = {}
        self._closed = False
        self.dropped_updates: Counter[str] = Counter()
        self.full_queues = 0
        self.send_timeouts = 0

    def start(self) -> None:
        self._closed = False

    async def broadcast(self, message: dict) -> None:
        # Todos os chamadores usam o mesmo loop ASGI. Enfileirar aqui nao faz I/O.
        for client in self._connections.values():
            if client.closing:
                continue
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.full_queues += 1
                self._record_drop(message)
                dropped = self._stop_client(client, 1013)
                logger.warning("[BASE] WEBSOCKET | websocket backlog full (%d updates discarded)", dropped + 1)

    async def serve(self, websocket: WebSocket, initial: tuple[dict, ...]) -> None:
        if self._closed or len(self._connections) >= _MAX_CLIENTS:
            logger.warning("[BASE] WEBSOCKET | websocket connection refused (hub closed or at capacity)")
            await self._close_socket(websocket, 1013)
            return

        client = _Client(asyncio.Queue(maxsize=_CLIENT_QUEUE_CAPACITY),
                         asyncio.get_running_loop().create_future())
        for message in initial:
            client.queue.put_nowait(message)
        self._connections[websocket] = client
        try:
            await asyncio.wait_for(websocket.accept(), timeout=_CLIENT_IO_TIMEOUT_SECONDS)
            writer = asyncio.create_task(self._write_client(websocket, client), name="vfence-websocket-writer")
            receiver = asyncio.create_task(self._read_client(websocket), name="vfence-websocket-reader")
            try:
                await asyncio.wait((writer, receiver), return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._stop_client(client, None if receiver.done() else 1001)
                receiver.cancel()
                for result in await asyncio.gather(writer, receiver, return_exceptions=True):
                    if isinstance(result, Exception) and not isinstance(result, WebSocketDisconnect):
                        logger.error("[BASE] WEBSOCKET | websocket client failed", exc_info=(type(result), result, result.__traceback__))
        except asyncio.TimeoutError:
            logger.warning("[BASE] WEBSOCKET | websocket handshake timed out")
            await self._close_socket(websocket, 1013)
        finally:
            self._discard_pending(client)
            self._connections.pop(websocket, None)
            client.done.set_result(None)

    async def close(self) -> None:
        self._closed = True
        clients = tuple(self._connections.values())
        dropped = sum(self._stop_client(client, 1001) for client in clients)
        if dropped:
            logger.info("[BASE] WEBSOCKET | websocket shutdown (%d queued updates discarded)", dropped)
        await asyncio.gather(*(asyncio.shield(client.done) for client in clients))

    @staticmethod
    async def _read_client(websocket: WebSocket) -> None:
        while True:
            await websocket.receive_text()

    async def _write_client(self, websocket: WebSocket, client: _Client) -> None:
        while (message := await client.queue.get()) is not None:
            try:
                await asyncio.wait_for(websocket.send_json(message), timeout=_CLIENT_IO_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self.send_timeouts += 1
                self._record_drop(message)
                dropped = self._stop_client(client, 1013)
                logger.warning("[BASE] WEBSOCKET | websocket send timed out (%d updates discarded)", dropped + 1)
                break
            except asyncio.CancelledError:
                self._record_drop(message)
                raise
            except (WebSocketDisconnect, OSError):
                self._record_drop(message)
                self._stop_client(client, None)
                logger.debug("[BASE] WEBSOCKET | websocket already disconnected", exc_info=True)
                break
            except Exception:
                self._record_drop(message)
                dropped = self._stop_client(client, 1011)
                logger.exception("[BASE] WEBSOCKET | websocket send failed (%d updates discarded)", dropped + 1)
                break
        if client.close_code is not None:
            await self._close_socket(websocket, client.close_code)

    @staticmethod
    async def _close_socket(websocket: WebSocket, code: int) -> None:
        try:
            await asyncio.wait_for(websocket.close(code=code), timeout=_CLIENT_IO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("[BASE] WEBSOCKET | websocket close timed out")
        except (WebSocketDisconnect, OSError):
            logger.debug("[BASE] WEBSOCKET | websocket already disconnected", exc_info=True)
        except Exception:
            logger.warning("[BASE] WEBSOCKET | websocket close failed", exc_info=True)

    def _stop_client(self, client: _Client, code: int | None) -> int:
        if client.closing:
            return 0
        client.closing = True
        client.close_code = code
        dropped = self._discard_pending(client)
        client.queue.put_nowait(None)
        return dropped

    def _discard_pending(self, client: _Client) -> int:
        dropped = 0
        while not client.queue.empty():
            message = client.queue.get_nowait()
            if message is not None:
                self._record_drop(message)
                dropped += 1
        return dropped

    def _record_drop(self, message: dict) -> None:
        update_type = message.get("type")
        key = update_type if isinstance(update_type, str) and update_type in _UPDATE_TYPES else "other"
        self.dropped_updates[key] += 1
