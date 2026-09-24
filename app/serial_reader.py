from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from itertools import cycle

from app.models import GatewayState
from app.protocol import ProtocolError, parse_message

logger = logging.getLogger("vfence.serial")
MOCK_MESSAGES = (
    "V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|1",
    "V1|COL01|POS|-31.306120|-54.063930|ATENCAO|11|1.10|2",
    "V1|COL01|POS|-31.306130|-54.063920|CRITICO|10|1.20|3",
    "V1|COL01|POS|-31.306140|-54.063910|FORA|9|1.50|4",
)


class SerialReader:
    def __init__(self, config, database, hub):
        self.config, self.database, self.hub = config, database, hub
        self.state = GatewayState(serial_port="mock" if config.mock_serial else config.serial_port)
        self._stop = asyncio.Event()

    async def run(self):
        await (self._run_mock() if self.config.mock_serial else self._run_serial())

    async def stop(self):
        self._stop.set()

    async def _run_mock(self):
        logger.info("Gateway simulado iniciado")
        self.state.status = "online"
        await self.hub.broadcast({"type": "gateway", "data": self.as_dict()})
        sequence = 0
        for template in cycle(MOCK_MESSAGES):
            if self._stop.is_set(): break
            sequence += 1
            parts = template.split("|"); parts[-1] = str(sequence)
            await self.process_line("|".join(parts))
            try: await asyncio.wait_for(self._stop.wait(), timeout=self.config.mock_interval_seconds)
            except asyncio.TimeoutError: pass

    async def _run_serial(self):
        while not self._stop.is_set():
            connection = None
            try:
                import serial
                logger.info("Tentando conectar em %s...", self.config.serial_port)
                connection = await asyncio.to_thread(serial.Serial, self.config.serial_port, self.config.serial_baud, timeout=1)
                self.state.status, self.state.error = "online", None
                logger.info("Gateway conectado em %s", self.config.serial_port)
                await self.hub.broadcast({"type": "gateway", "data": self.as_dict()})
                while not self._stop.is_set():
                    line = await asyncio.to_thread(connection.readline)
                    if line: await self.process_line(line)
            except asyncio.CancelledError: raise
            except Exception as exc:
                if self.state.status != "offline" or self.state.error != str(exc):
                    logger.warning("Gateway desconectado: %s", exc)
                self.state.status, self.state.error = "offline", str(exc)
                await self.hub.broadcast({"type": "gateway", "data": self.as_dict()})
            finally:
                if connection is not None: await asyncio.to_thread(connection.close)
            try: await asyncio.wait_for(self._stop.wait(), timeout=self.config.reconnect_seconds)
            except asyncio.TimeoutError: pass

    async def process_line(self, line):
        try:
            packet = parse_message(line, max_length=self.config.max_line_length)
            if packet is None: return
            logger.info("[RX] %s", packet.raw_message)
            collar, event = await asyncio.to_thread(self.database.save_packet, packet)
            self.state.last_message = packet.raw_message
            self.state.last_seen = datetime.now(timezone.utc).isoformat()
            if event: logger.info("%s: %s -> %s", event["collar_id"], event["old_value"] or "—", event["new_value"])
            await self.hub.broadcast({"type": "telemetry", "data": packet.as_dict(), "collar": collar, "event": event})
        except ProtocolError as exc:
            logger.warning("Pacote ignorado: %s", exc)

    def as_dict(self):
        return {"status": self.state.status, "serial_port": self.state.serial_port,
                "last_message": self.state.last_message, "last_seen": self.state.last_seen,
                "error": self.state.error, "mock": self.config.mock_serial}
