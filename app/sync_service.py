from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import httpx

from app.central_client import (
    CentralAuthenticationError,
    CentralClient,
    CentralClientError,
    InvalidFenceError,
)
from app.terminal import base_block, base_value, gateway_log_field

logger = logging.getLogger("central_sync")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def central_error_rows(error: Exception, *, detail: bool) -> list[str]:
    cause = error.__cause__ or error
    status = error.status_code if isinstance(error, CentralClientError) else None
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
    if isinstance(error, CentralAuthenticationError):
        reason = "AUTENTICACAO RECUSADA"
    elif isinstance(cause, (httpx.TimeoutException, TimeoutError)):
        reason = "TIMEOUT"
    elif isinstance(cause, (httpx.NetworkError, ConnectionError)):
        reason = "FALHA DE CONEXAO"
    elif isinstance(error, (InvalidFenceError, ValueError, KeyError)) or (
        isinstance(error, CentralClientError) and error.invalid_response
    ):
        reason = "ERRO DE VALIDACAO"
    elif isinstance(cause, sqlite3.Error):
        reason = "FALHA SQLITE"
    elif isinstance(cause, httpx.HTTPStatusError) or status is not None:
        reason = "ERRO HTTP"
    else:
        reason = "ERRO CENTRAL"
    if type(status) is int and 100 <= status <= 599:
        reason += f" | HTTP {status}"
    rows = [f"Motivo: {reason}"]
    if detail:
        technical = type(error).__name__
        if cause is not error:
            technical += f" | Causa: {type(cause).__name__}"
        sqlite_code = getattr(cause, "sqlite_errorcode", None)
        if isinstance(cause, sqlite3.Error) and type(sqlite_code) is int:
            technical += f" | Codigo SQLite: {sqlite_code}"
        rows.append(f"Tipo: {technical}")
    return rows


class CentralSyncService:
    def __init__(self, config, database, serial_reader, hub, client: CentralClient | None = None):
        self.config = config
        self.terminal_detail = getattr(config, "terminal_detail", False)
        self.database = database
        self.serial_reader = serial_reader
        self.hub = hub
        self.client = client or CentralClient(
            config.central_base_url,
            config.base_token,
            config.central_timeout_seconds,
        )
        self._stop = asyncio.Event()
        self.status = "disabled" if not config.central_sync_enabled else "offline"
        self.last_error: str | None = None
        self.last_sync: str | None = None

    def as_dict(self) -> dict:
        try:
            health = self.database.outbox_health()
        except sqlite3.Error:
            health = {"outbox_size": None, "outbox_oldest_at": None, "outbox_pressure": None}
        return {
            "enabled": self.config.central_sync_enabled,
            "status": self.status,
            "last_error": self.last_error,
            "last_sync": self.last_sync,
            "base_id": self.config.base_id,
            "url": self.config.central_base_url,
            **health,
        }

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.config.central_sync_enabled:
            logger.info("[BASE] CENTRAL | SINCRONIZACAO DESATIVADA")
            return
        if not self.config.base_token:
            self.status = "error"
            self.last_error = "BASE_TOKEN ausente"
            logger.error("%s", base_block(
                "ERRO | CENTRAL", "Estado: TOKEN DA BASE AUSENTE", critical=True,
            ))
            return
        sqlite_retries = 0
        while not self._stop.is_set():
            try:
                delay = await self._run_sync_iteration()
                sqlite_retries = 0
            except sqlite3.Error as exc:
                sqlite_retries = min(sqlite_retries + 1, 7)
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                status = "storage_full" if code == sqlite3.SQLITE_FULL else "storage_unavailable"
                if self.status != status:
                    logger.error("%s", base_block(
                        "ERRO | CENTRAL", *central_error_rows(exc, detail=self.terminal_detail), critical=True,
                    ))
                self.status = status
                self.last_error = type(exc).__name__
                delay = min(60, max(0.01, self.config.central_retry_seconds) * (2 ** (sqlite_retries - 1)))
                await self.hub.broadcast({"type": "central", "data": await asyncio.to_thread(self.as_dict)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run_sync_iteration(self) -> float:
        try:
            await self.sync_once()
            await self._set_online()
            return self.config.central_sync_interval_seconds
        except CentralAuthenticationError as exc:
            await self._set_offline(f"authentication: {exc}", exc)
            return max(self.config.central_retry_seconds, 30)
        except (CentralClientError, InvalidFenceError, ValueError, KeyError) as exc:
            await self._set_offline(str(exc), exc)
            return self.config.central_retry_seconds

    async def _set_online(self) -> None:
        changed = self.status != "online"
        self.status = "online"
        self.last_error = None
        self.last_sync = utc_now()
        if changed:
            logger.info("%s", base_block("CENTRAL | SINCRONIZACAO", "Estado: CICLO CONCLUIDO"))
            await asyncio.to_thread(
                self.database.enqueue_event, "central_online",
                timestamp=self.last_sync, detail=self.config.central_base_url,
            )
        await self.hub.broadcast({"type": "central", "data": self.as_dict()})

    async def _set_offline(self, error: str, exception: Exception) -> None:
        changed = self.status != "offline" or self.last_error != error
        self.status = "offline"
        self.last_error = error
        if changed:
            logger.warning("%s", base_block(
                "AVISO | CENTRAL", "Estado: CICLO INDISPONIVEL",
                *central_error_rows(exception, detail=self.terminal_detail), critical=True,
            ))
            await asyncio.to_thread(
                self.database.enqueue_event, "central_offline",
                timestamp=utc_now(), detail=error[:500],
            )
        await self.hub.broadcast({"type": "central", "data": self.as_dict()})

    async def sync_once(self) -> None:
        state = await asyncio.to_thread(self.database.fence_state)
        reported_version = state.get("downloaded_version")
        queued = await asyncio.to_thread(self.database.outbox_size)
        heartbeat = await self.client.heartbeat(
            base_id=self.config.base_id,
            fence_version=reported_version,
            serial_status=self.serial_reader.state.status,
            queue_size=queued,
        )
        desired = heartbeat["desired_fence_version"]
        await asyncio.to_thread(self.database.set_desired_fence, desired, utc_now())
        if self.terminal_detail and queued:
            logger.info("[BASE] CENTRAL | Outbox pendente no heartbeat: %d", queued)

        if desired is not None and desired != state.get("downloaded_version"):
            display_version = base_value(gateway_log_field("fenceVersion", desired))
            display_local = base_value(gateway_log_field("fenceVersion", reported_version))
            logger.info("%s", base_block(
                f"CERCA | VERSAO {display_version}",
                f"Etapa: PUBLICACAO DETECTADA NA CENTRAL | Versao local: {display_local}",
            ))
            fence = await self.client.download_fence(int(desired))
            if self.terminal_detail:
                logger.info("[BASE] CERCA | Download validado versao=%s CRC=%s",
                            display_version, base_value(fence["crc32"]))
            await asyncio.to_thread(self.database.save_pending_fence, fence, utc_now())
            logger.info("%s", base_block(
                f"CERCA | VERSAO {display_version}",
                f"Download: VALIDADO | Pontos: {len(fence['points_e6'])}",
                "SQLite: SALVA NA BASE | Estado: AGUARDANDO ENTREGA",
            ))
            await asyncio.to_thread(
                self.database.enqueue_event, "fence_downloaded",
                timestamp=utc_now(), detail=f"version={desired}",
            )
            await self._deliver_fence(fence)
            await self._broadcast_fence()
        elif state.get("pending_version") is not None:
            fence = await asyncio.to_thread(
                self.database.fence_by_version, int(state["pending_version"])
            )
            if fence:
                fence["margin_attention_cm"] = state["margin_attention_cm"]
                fence["margin_critical_cm"] = state["margin_critical_cm"]
                await self._deliver_fence(fence)
                await self._broadcast_fence()

        await self._flush_outbox()

    async def _broadcast_fence(self) -> None:
        snapshot = await asyncio.to_thread(self.database.fence_snapshot)
        await self.hub.broadcast({"type": "fence", "data": snapshot})

    async def _deliver_fence(self, fence: dict) -> None:
        version = int(fence["version"])
        crc = fence["crc32"]
        confirmed = await asyncio.to_thread(self.database.confirmed_recipients, version, crc)
        all_confirmed = True
        for collar_id in self.config.known_collar_ids:
            if collar_id in confirmed:
                continue
            now = utc_now()
            await asyncio.to_thread(
                self.database.enqueue_delivery, collar_id, version, "transmitting",
                now,
            )
            await asyncio.to_thread(
                self.database.set_fence_progress,
                sent_version=version, timestamp=now,
            )
            success, detail = await self.serial_reader.send_fence(collar_id, fence)
            now = utc_now()
            if success:
                await asyncio.to_thread(
                    self.database.enqueue_delivery, collar_id, version, "confirmed",
                    now, crc32=crc,
                )
            else:
                all_confirmed = False
                await asyncio.to_thread(
                    self.database.enqueue_delivery, collar_id, version, "failed",
                    now, detail=detail,
                )
                await asyncio.to_thread(
                    self.database.set_fence_progress,
                    error=f"{collar_id}: {detail}", timestamp=now,
                )
                status = "CONFIRMACAO NAO RECEBIDA" if detail == "gateway_confirmation_timeout" else "FALHA REGISTRADA NA BASE"
                safe_detail = gateway_log_field("detail", detail)
                logger.warning("%s", base_block(
                    f"CERCA | VERSAO {version}", f"Entrega: {status}", f"Motivo: {base_value(safe_detail)}",
                    collar_id=collar_id, critical=True,
                ))
        if all_confirmed and self.config.known_collar_ids:
            await asyncio.to_thread(
                self.database.set_fence_progress,
                confirmed_version=version, error=None, timestamp=utc_now(),
            )
            simulated = getattr(self.config, "mock_serial", False)
            logger.info("%s", base_block(
                f"CERCA | VERSAO {version}",
                f"Entrega: {'SIMULADA' if simulated else 'CONFIRMADA PARA AS COLEIRAS CONFIGURADAS'}",
                "SQLite: RESULTADO REGISTRADO",
            ))

    async def _flush_outbox(self) -> None:
        generation, items = await asyncio.to_thread(
            self.database.outbox_snapshot, max(1, min(self.config.central_batch_size, 500))
        )
        if not items:
            return
        acked = await self.client.send_batch_confirmed(self.config.base_id, generation, items)
        if self.terminal_detail:
            logger.info("[BASE] CENTRAL | ACK de lote recebido edge_seq=%d | Itens no lote: %d", acked[-1], len(acked))
        removed = await asyncio.to_thread(self.database.acknowledge_confirmed_batch, generation, acked)
        logger.info("%s", base_block(
            "CENTRAL | OUTBOX", "Estado: LOTE CONFIRMADO PELA CENTRAL",
            f"Outbox: ATUALIZADA | Itens removidos: {removed}",
        ))
