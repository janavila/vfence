from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from itertools import cycle

from app.models import GatewayState
from app.protocol import ParsedPacket, ProtocolError, parse_message

logger = logging.getLogger("serial_reader")

MOCK_MESSAGES = (
    "V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|1",
    "V1|COL01|POS|-31.306120|-54.063930|ATENCAO|11|1.10|2",
    "V1|COL01|POS|-31.306130|-54.063920|CRITICO|10|1.20|3",
    "V1|COL01|POS|-31.306140|-54.063910|FORA|9|1.50|4",
)


class SerialReader:
    def __init__(self, config, database, hub):
        self.config = config
        self.database = database
        self.hub = hub
        self.state = GatewayState(
            serial_port="mock" if config.mock_serial else config.serial_port
        )
        self._stop = asyncio.Event()
        self._connection = None
        self._write_lock = asyncio.Lock()
        self._fence_lock = asyncio.Lock()
        self._fence_result: asyncio.Future | None = None

    async def run(self):
        if self.config.mock_serial:
            await self._run_mock()
        else:
            await self._run_serial()

    async def stop(self):
        self._stop.set()

    async def _run_mock(self):
        logger.info("mock gateway started")
        self.state.status = "online"
        await self.hub.broadcast({"type": "gateway", "data": self.as_dict()})

        sequence = 0
        for template in cycle(MOCK_MESSAGES):
            if self._stop.is_set():
                break
            sequence += 1
            fields = template.split("|")
            fields[-1] = str(sequence)
            await self.process_line("|".join(fields))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.mock_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _run_serial(self):
        while not self._stop.is_set():
            connection = None
            try:
                import serial

                logger.info("connection attempt (%s)", self.config.serial_port)
                connection = await asyncio.to_thread(
                    serial.Serial,
                    self.config.serial_port,
                    self.config.serial_baud,
                    timeout=1,
                )
                self._connection = connection
                self.state.status = "online"
                self.state.error = None
                logger.info("gateway connected (%s)", self.config.serial_port)
                if self.config.central_sync_enabled:
                    try:
                        await asyncio.to_thread(
                            self.database.enqueue_event, "gateway_online",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            detail=self.config.serial_port,
                        )
                    except Exception:
                        logger.exception("failed to queue gateway_online")
                await self.hub.broadcast(
                    {"type": "gateway", "data": self.as_dict()}
                )

                while not self._stop.is_set():
                    line = await asyncio.to_thread(connection.readline)
                    if line:
                        await self.process_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state_changed = self.state.status != "offline" or self.state.error != str(exc)
                if state_changed:
                    logger.warning("gateway offline (%s)", exc)
                    if self.config.central_sync_enabled:
                        try:
                            await asyncio.to_thread(
                                self.database.enqueue_event, "gateway_offline",
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                detail=str(exc)[:500],
                            )
                        except Exception:
                            logger.exception("failed to queue gateway_offline")
                self.state.status = "offline"
                self.state.error = str(exc)
                await self.hub.broadcast(
                    {"type": "gateway", "data": self.as_dict()}
                )
            finally:
                self._connection = None
                if self._fence_result and not self._fence_result.done():
                    self._fence_result.set_result((False, "serial_disconnected"))
                if connection is not None:
                    await asyncio.to_thread(connection.close)

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.reconnect_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def process_line(self, line):
        try:
            text = line.decode("utf-8").strip() if isinstance(line, bytes) else str(line).strip()
            if self._handle_fence_status(text):
                return
            app_packet = self._parse_gateway_app(text)
            if app_packet is not None:
                if isinstance(app_packet, ParsedPacket):
                    await self._save_and_broadcast(app_packet)
                else:
                    await self._process_gateway_event(app_packet)
                return
            packet = parse_message(line, max_length=self.config.max_line_length)
            if packet is None:
                return
            await self._save_and_broadcast(packet)
        except ProtocolError as exc:
            logger.warning("packet rejected (%s)", exc)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("gateway application line rejected (%s)", exc)

    async def _save_and_broadcast(self, packet: ParsedPacket):
        logger.info("packet received (%s)", packet.raw_message)
        collar, event = await asyncio.to_thread(
            self.database.save_packet, packet, self.config.central_sync_enabled
        )
        self.state.last_message = packet.raw_message
        self.state.last_seen = datetime.now(timezone.utc).isoformat()
        if event:
            logger.info("zone transition (%s, %s -> %s)",
                        event["collar_id"], event["old_value"] or "none",
                        event["new_value"])
        await self.hub.broadcast({
            "type": "telemetry", "data": packet.as_dict(),
            "collar": collar, "event": event,
        })

    @staticmethod
    def _collar_name(value) -> str:
        return f"COL{int(value):02d}"

    def _parse_gateway_app(self, line: str):
        if not line.startswith("[APP] "):
            return None
        data = json.loads(line[6:])
        if data.get("type") == "telemetry":
            gps_valid = bool(data.get("gpsValid"))
            inside = bool(data.get("inside"))
            if not gps_valid:
                zone = "GNSS_INVALIDO"
            elif not inside:
                zone = "FORA"
            elif data.get("veryNear"):
                zone = "CRITICO"
            elif data.get("near"):
                zone = "ATENCAO"
            else:
                zone = "SEGURO"
            return ParsedPacket(
                version="GATEWAY_V1",
                collar_id=self._collar_name(data["collarId"]),
                type="POS",
                timestamp=datetime.now(timezone.utc),
                raw_message=line,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                zone=zone,
                satellites=data.get("satellites"),
                hdop=data.get("hdop"),
                rssi=data.get("rssi"),
                snr=data.get("snr"),
                sequence=int(data["sequence"]),
                battery_pct=data.get("battery"),
                fence_version=data.get("fenceVersion"),
            )
        if data.get("type") == "event":
            return data
        raise ValueError("tipo [APP] desconhecido")

    async def _process_gateway_event(self, data: dict):
        timestamp = datetime.now(timezone.utc).isoformat()
        collar_id = self._collar_name(data["collarId"]) if "collarId" in data else None
        if self.config.central_sync_enabled:
            await asyncio.to_thread(
                self.database.enqueue_event,
                str(data.get("event", "gateway_event")).lower(),
                timestamp=timestamp,
                collar_id=collar_id,
                detail=json.dumps(data, separators=(",", ":")),
            )
        await self.hub.broadcast({"type": "gateway_event", "data": data})

    def _handle_fence_status(self, line: str) -> bool:
        if not line.startswith("[FENCE TX]"):
            return False
        if self._fence_result and not self._fence_result.done():
            if "SUCESSO:" in line:
                self._fence_result.set_result((True, "collar_confirmed"))
            elif "transferencia abortada:" in line:
                self._fence_result.set_result((False, line.split(":", 1)[-1].strip()))
        return True

    async def send_command(self, command: str) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("comando Serial deve ocupar uma única linha")
        if self.config.mock_serial:
            logger.info("mock serial command (%s)", command)
            return
        async with self._write_lock:
            connection = self._connection
            if connection is None or not getattr(connection, "is_open", True):
                raise ConnectionError("gateway offline")
            await asyncio.to_thread(connection.write, (command + "\n").encode("ascii"))
            await asyncio.to_thread(connection.flush)

    @staticmethod
    def _numeric_collar_id(collar_id: str) -> int:
        match = re.fullmatch(r"(?:COL)?(\d+)", collar_id.upper())
        if not match or not 1 <= int(match.group(1)) <= 254:
            raise ValueError(f"identificador incompatível com gateway: {collar_id}")
        return int(match.group(1))

    async def send_fence(self, collar_id: str, fence: dict,
                         timeout_seconds: float = 45.0) -> tuple[bool, str]:
        async with self._fence_lock:
            numeric_id = self._numeric_collar_id(collar_id)
            if self.config.mock_serial:
                logger.info("mock fence confirmed (collar=%s version=%s)",
                            collar_id, fence["version"])
                return True, "mock_confirmed"
            loop = asyncio.get_running_loop()
            self._fence_result = loop.create_future()
            try:
                critical_m = max(0, round(int(fence["margin_critical_cm"]) / 100))
                attention_m = max(critical_m, round(int(fence["margin_attention_cm"]) / 100))
                await self.send_command(
                    f"PARAM {numeric_id} {critical_m} {attention_m} "
                    f"{self.config.collar_telemetry_seconds} "
                    f"{self.config.collar_gps_timeout_seconds} "
                    f"{self.config.collar_low_battery_pct} "
                    f"{self.config.collar_critical_battery_pct}"
                )
                points = fence["points_e6"]
                await self.send_command(
                    f"FENCE_NEW {numeric_id} {int(fence['version'])} {len(points)}"
                )
                for index, (latitude_e6, longitude_e6) in enumerate(points):
                    await self.send_command(
                        f"FENCE_POINT {index} {latitude_e6 / 1_000_000:.7f} "
                        f"{longitude_e6 / 1_000_000:.7f}"
                    )
                await self.send_command("FENCE_SEND")
                return await asyncio.wait_for(self._fence_result, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                return False, "gateway_confirmation_timeout"
            finally:
                self._fence_result = None

    def as_dict(self):
        return {
            "status": self.state.status,
            "serial_port": self.state.serial_port,
            "last_message": self.state.last_message,
            "last_seen": self.state.last_seen,
            "error": self.state.error,
            "mock": self.config.mock_serial,
        }
