from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.central_client import (
    CentralAuthenticationError,
    CentralClient,
    CentralClientError,
    InvalidFenceError,
)

logger = logging.getLogger("central_sync")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CentralSyncService:
    def __init__(self, config, database, serial_reader, hub, client: CentralClient | None = None):
        self.config = config
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
        return {
            "enabled": self.config.central_sync_enabled,
            "status": self.status,
            "last_error": self.last_error,
            "last_sync": self.last_sync,
            "base_id": self.config.base_id,
            "url": self.config.central_base_url,
            "outbox_size": self.database.outbox_size(),
        }

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.config.central_sync_enabled:
            logger.info("central synchronization disabled")
            return
        if not self.config.base_token:
            self.status = "error"
            self.last_error = "BASE_TOKEN ausente"
            logger.error(self.last_error)
            return
        while not self._stop.is_set():
            try:
                await self.sync_once()
                await self._set_online()
                delay = self.config.central_sync_interval_seconds
            except CentralAuthenticationError as exc:
                await self._set_offline(f"authentication: {exc}")
                delay = max(self.config.central_retry_seconds, 30)
            except (CentralClientError, InvalidFenceError, ValueError, KeyError) as exc:
                await self._set_offline(str(exc))
                delay = self.config.central_retry_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _set_online(self) -> None:
        changed = self.status != "online"
        self.status = "online"
        self.last_error = None
        self.last_sync = utc_now()
        if changed:
            logger.info("central connected")
            await asyncio.to_thread(
                self.database.enqueue_event, "central_online",
                timestamp=self.last_sync, detail=self.config.central_base_url,
            )
        await self.hub.broadcast({"type": "central", "data": self.as_dict()})

    async def _set_offline(self, error: str) -> None:
        changed = self.status != "offline" or self.last_error != error
        self.status = "offline"
        self.last_error = error
        if changed:
            logger.warning("central unavailable (%s)", error)
            await asyncio.to_thread(
                self.database.enqueue_event, "central_offline",
                timestamp=utc_now(), detail=error[:500],
            )
        await self.hub.broadcast({"type": "central", "data": self.as_dict()})

    async def sync_once(self) -> None:
        state = await asyncio.to_thread(self.database.fence_state)
        reported_version = state.get("downloaded_version")
        heartbeat = await self.client.heartbeat(
            base_id=self.config.base_id,
            fence_version=reported_version,
            serial_status=self.serial_reader.state.status,
            queue_size=await asyncio.to_thread(self.database.outbox_size),
        )
        desired = heartbeat["desired_fence_version"]
        await asyncio.to_thread(self.database.set_desired_fence, desired, utc_now())

        if desired is not None and desired != state.get("downloaded_version"):
            logger.info(
                "new fence detected (local=%s desired=%s)",
                state.get("downloaded_version"), desired,
            )
            fence = await self.client.download_fence(int(desired))
            logger.info(
                "fence downloaded and validated (version=%s points=%s crc=%s)",
                desired, len(fence["points_e6"]), fence["crc32"],
            )
            await asyncio.to_thread(self.database.save_pending_fence, fence, utc_now())
            logger.info(
                "fence persisted locally (version=%s points=%s)",
                desired, len(fence["points_e6"]),
            )
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
        all_confirmed = True
        for collar_id in self.config.known_collar_ids:
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
        if all_confirmed and self.config.known_collar_ids:
            await asyncio.to_thread(
                self.database.set_fence_progress,
                confirmed_version=version, error=None, timestamp=utc_now(),
            )
            logger.info("fence available locally (version=%s)", version)

    async def _flush_outbox(self) -> None:
        items = await asyncio.to_thread(
            self.database.outbox_batch, self.config.central_batch_size
        )
        if not items:
            return
        acked = await self.client.send_batch(self.config.base_id, items)
        await asyncio.to_thread(self.database.acknowledge_outbox, acked)
