from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Path as RoutePath, Query, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Database
from app.realtime import RealtimeHub
from app.serial_reader import SerialReader
from app.sync_service import CentralSyncService
from app.terminal import RULE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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
    logger.info(
        "\n%s\n VFENCE | BASE LOCAL | INICIALIZACAO\n%s\n"
        "[OK] Servidor iniciado | SQLite inicializado\n"
        "[INFO] Fonte=%s | ACK de medias=%s | Terminal=%s\n%s",
        RULE, RULE, "mock" if settings.mock_serial else "Serial USB",
        "HABILITADO" if settings.metrics_serial_ack_enabled and not settings.mock_serial else "DESATIVADO",
        "DETALHADO" if settings.terminal_detail else "NORMAL", RULE,
    )
    task = asyncio.create_task(reader.run(), name="vfence-serial-reader")
    sync_task = asyncio.create_task(sync_service.run(), name="vfence-central-sync")
    try:
        yield
    finally:
        logger.info("service stopping")
        await reader.stop()
        await sync_service.stop()
        task.cancel()
        sync_task.cancel()
        try:
            await hub.close()
        finally:
            for result in await asyncio.gather(task, sync_task, return_exceptions=True):
                if isinstance(result, Exception):
                    logger.error("background service failed", exc_info=(type(result), result, result.__traceback__))


app = FastAPI(title="VFence Monitor API", version="1.0.0", lifespan=lifespan)
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


@app.get("/api/central")
async def central():
    return await asyncio.to_thread(sync_service.as_dict)


@app.get("/api/fence-state")
async def fence_state():
    return await asyncio.to_thread(database.fence_snapshot)


@app.get("/api/collars")
async def collars():
    return await asyncio.to_thread(database.list_collars)


@app.get("/api/collars/{collar_id}")
async def collar(collar_id: str):
    result = await asyncio.to_thread(database.get_collar, collar_id)
    if result is None:
        raise HTTPException(status_code=404, detail="collar_not_found")
    return result


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
