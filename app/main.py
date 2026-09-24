from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Database
from app.realtime import RealtimeHub
from app.serial_reader import SerialReader

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("vfence")
BASE_DIR = Path(__file__).resolve().parent
database = Database(settings.database_url)
hub = RealtimeHub()
reader = SerialReader(settings, database, hub)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    logger.info("Servidor iniciado")
    task = asyncio.create_task(reader.run(), name="vfence-serial-reader")
    yield
    await reader.stop()
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass


app = FastAPI(title="VFence", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard(): return FileResponse(BASE_DIR / "templates" / "index.html")

@app.get("/api/health")
async def health(): return {"status": "ok", "gateway": reader.state.status, "serial_port": reader.state.serial_port}

@app.get("/api/gateway")
async def gateway(): return reader.as_dict()

@app.get("/api/collars")
async def collars(): return await asyncio.to_thread(database.list_collars)

@app.get("/api/collars/{collar_id}")
async def collar(collar_id: str):
    result = await asyncio.to_thread(database.get_collar, collar_id)
    if result is None: raise HTTPException(status_code=404, detail="Coleira não encontrada")
    return result

@app.get("/api/collars/{collar_id}/telemetry")
async def telemetry(collar_id: str, limit: int = Query(100, ge=1, le=1000)):
    if await asyncio.to_thread(database.get_collar, collar_id) is None:
        raise HTTPException(status_code=404, detail="Coleira não encontrada")
    return await asyncio.to_thread(database.telemetry, collar_id, limit)

@app.get("/api/events")
async def events(limit: int = Query(100, ge=1, le=1000)):
    return await asyncio.to_thread(database.events, limit)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "gateway", "data": reader.as_dict()})
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
