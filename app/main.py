from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Path as RoutePath, Query, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Database
from app.profile_api import router as profile_router
from app.realtime import RealtimeHub
from app.serial_reader import SerialReader
from app.sync_service import CentralSyncService, central_error_rows
from app.terminal import RULE, base_block, base_value

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# HTTPX's request log includes the URL, which can contain sensitive parameters.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("server")

BASE_DIR = Path(__file__).resolve().parent
database = Database(settings.database_url)
hub = RealtimeHub()
reader = SerialReader(settings, database, hub)
sync_service = CentralSyncService(settings, database, reader, hub)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    hub.start()
    serial_state = "SIMULADA" if settings.mock_serial else "CONFIGURADA | AGUARDANDO ABERTURA"
    logger.info(
        "\n%s\n VFENCE | RASPBERRY PI | BASE LOCAL\n%s\n"
        " Serial: %s | Baud: %d | Estado: %s\n"
        " SQLite: INICIALIZADO | Central: SINCRONIZACAO %s\n"
        " ACK de medias: %s | Terminal: %s\n%s",
        RULE, RULE, base_value("mock" if settings.mock_serial else settings.serial_port),
        settings.serial_baud, serial_state, "CONFIGURADA" if settings.central_sync_enabled else "DESATIVADA",
        "HABILITADO" if settings.metrics_serial_ack_enabled and not settings.mock_serial else "DESATIVADO",
        "DETALHADO" if settings.terminal_detail else "NORMAL", RULE,
    )
    task = asyncio.create_task(reader.run(), name="vfence-serial-reader")
    sync_task = asyncio.create_task(sync_service.run(), name="vfence-central-sync")
    try:
        yield
    finally:
        logger.info("[BASE] ENCERRAMENTO | servico finalizando")
        await reader.stop()
        await sync_service.stop()
        task.cancel()
        sync_task.cancel()
        try:
            await hub.close()
        finally:
            serial_result, central_result = await asyncio.gather(task, sync_task, return_exceptions=True)
            if isinstance(serial_result, Exception):
                logger.error("%s", base_block(
                    "ERRO | SERVICO EM SEGUNDO PLANO", f"Causa: {type(serial_result).__name__}", critical=True,
                ), exc_info=(type(serial_result), serial_result, serial_result.__traceback__)
                    if settings.terminal_detail else None)
            if isinstance(central_result, Exception):
                logger.error("%s", base_block(
                    "ERRO | SERVICO EM SEGUNDO PLANO | CENTRAL",
                    *central_error_rows(central_result, detail=settings.terminal_detail), critical=True,
                ))


app = FastAPI(title="VFence Monitor API", version="1.0.0", lifespan=lifespan)
app.include_router(profile_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/fences", include_in_schema=False)
async def fence_editor():
    return FileResponse(BASE_DIR / "templates" / "fence.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "gateway": reader.state.status,
        "serial_port": reader.state.serial_port,
    }


@app.get("/api/gateway")
async def gateway():
    return reader.as_dict()


@app.get("/api/gateway/profile")
async def gateway_profile():
    if settings.mock_serial or reader.state.status != "online":
        raise HTTPException(status_code=503, detail="gateway_unavailable")
    try:
        result = await reader.send_gateway_command("GET_PROFILE")
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="gateway_profile_timeout") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="gateway_unavailable") from exc
    if (result.get("command") != "GET_PROFILE" or result.get("profileControlVersion") != 1
            or not result.get("ok")):
        raise HTTPException(status_code=501, detail="gateway_profile_unsupported")
    return result


@app.get("/api/collars/{collar_id}/pdr")
async def collar_pdr(collar_id: int = RoutePath(ge=1, le=254), limit: int = Query(60, ge=1, le=120)):
    return await asyncio.to_thread(database.list_pdr, collar_id, limit)


@app.get("/api/collars/{collar_id}/link-metrics")
async def collar_link_metrics(collar_id: int = RoutePath(ge=1, le=254), limit: int = Query(1, ge=1, le=120)):
    snapshot = await asyncio.to_thread(database.link_metrics, collar_id, limit)
    now = datetime.now(timezone.utc)

    def age_ms(stamp: str) -> int:
        recorded = datetime.fromisoformat(stamp)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        return max(0, int((now - recorded).total_seconds() * 1000))

    instant = snapshot["instant"]
    if instant is not None:
        instant["received_at"] = instant.pop("timestamp")
        instant["age_ms"] = age_ms(instant["received_at"])
        for key in ("hdop", "rssi", "snr"):
            value = instant[key]
            valid = value is not None and math.isfinite(value)
            if key == "hdop" and instant["zone"] == "GNSS_INVALIDO":
                valid = False
            instant[key] = value if valid else None
            instant[f"{key}_valid"] = valid
    for kind, rows in snapshot["windows"].items():
        for row in rows:
            row["age_ms"] = age_ms(row["persisted_at"])
            for field in ("hdop_mean_valid", "rssi_mean_valid", "snr_mean_valid"):
                if row.get(field) is not None:
                    row[field] = bool(row[field])
            if kind != "pdr":
                row["invalid_reason"] = "no_samples" if row["sample_count"] == 0 else None
    profile = reader.effective_profile if reader.state.status == "online" else None
    if profile:
        profile = {**profile, "age_ms": age_ms(profile["observed_at"])}
    return {
        "schema": "vfence.base.link_metrics.v1", "collar_id": collar_id, "as_of": now.isoformat(),
        "gateway": {"status": reader.state.status, "effective_profile": profile},
        "pdr_basis": "observed_ordinal_interval", **snapshot,
    }


@app.get("/api/central")
async def central():
    return await asyncio.to_thread(sync_service.as_dict)


@app.get("/api/fence-state")
async def fence_state():
    return await asyncio.to_thread(database.fence_snapshot)


@app.get("/api/collars")
async def collars():
    rows = await asyncio.to_thread(database.list_collars)
    return [reader.collar_snapshot(row) for row in rows]


@app.get("/api/collars/{collar_id}")
async def collar(collar_id: str):
    result = await asyncio.to_thread(database.get_collar, collar_id)
    if result is None:
        raise HTTPException(status_code=404, detail="collar_not_found")
    return reader.collar_snapshot(result)


@app.get("/api/collars/{collar_id}/telemetry")
async def telemetry(collar_id: str, limit: int = Query(100, ge=1, le=1000)):
    if await asyncio.to_thread(database.get_collar, collar_id) is None:
        raise HTTPException(status_code=404, detail="collar_not_found")
    return await asyncio.to_thread(database.telemetry, collar_id, limit)


@app.get("/api/events")
async def events(limit: int = Query(100, ge=1, le=1000)):
    return await asyncio.to_thread(database.events, limit)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    gateway_state = reader.as_dict()
    fence = await asyncio.to_thread(database.fence_snapshot)
    await hub.serve(websocket, (
        {"type": "gateway", "data": gateway_state},
        {"type": "fence", "data": fence},
    ))
