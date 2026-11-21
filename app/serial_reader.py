from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from functools import partial
from itertools import count, cycle

from app.database import MetricSaveResult
from app.metric_protocol import (
    HDOP_SUMMARY_KIND,
    METRIC_LINE_MAX_BYTES,
    METRIC_SUMMARY_KINDS,
    RADIO_SUMMARY_KIND,
    MetricSummary,
    parse_metric_summary,
)
from app.models import GatewayState
from app.pdr_protocol import PDR_KIND, PDR_SCHEMA, PdrSummary, parse_pdr_summary
from app.protocol import ParsedPacket, ProtocolError, parse_message
from app.terminal import (
    RULE, collar_label, event_block, metric_block, metric_identity, position_line, value_text,
    zone_transition_alert,
)

logger = logging.getLogger("serial_reader")

GATEWAY_SERIAL_SCHEMA = "vfence.gateway.serial.v1"
_APP_PREFIX = b"[APP] "
_SERIAL_READ_BYTES = 256
# Deixa margem para respostas que incluem texto de comandos de ate 512 caracteres.
_LEGACY_LINE_MAX_BYTES = 4096
_WORK_QUEUE_CAPACITY = 128
_METRIC_QUEUE_CAPACITY = 64
_PDR_QUEUE_CAPACITY = 8
_WORK_DRAIN_SECONDS = 2.0
_LEGACY_WORK_PRIORITY = 0
_METRIC_WORK_PRIORITY = 1
_PDR_WORK_PRIORITY = 2
_METRIC_ACK_CAPACITY = 16
_METRIC_ACK_WRITE_SECONDS = 0.25
_SERIAL_RESYNC = b"\x00\n"
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_JSON_TOKEN = re.compile(_JSON_STRING + r"|[\[\]{}]")
_JSON_STRING_MEMBER = re.compile(r"\s*:\s*(" + _JSON_STRING + ")")
_IMMEDIATE_APP_KINDS = {"command_response", "fence_transfer"}
_FRAMED_SUMMARIES = METRIC_SUMMARY_KINDS | {PDR_KIND}
_WorkItem = tuple[int, int, Callable[[], Awaitable[bool | None]] | None]

GATEWAY_APP_KINDS = {
    "telemetry", "event", "control", "collar_state", "collar_status",
    "gateway_state", "radio_config", "command_response", "fence_transfer",
}

MOCK_MESSAGES = (
    "V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|1",
    "V1|COL01|POS|-31.306120|-54.063930|ATENCAO|11|1.10|2",
    "V1|COL01|POS|-31.306130|-54.063920|CRITICO|10|1.20|3",
    "V1|COL01|POS|-31.306140|-54.063910|FORA|9|1.50|4",
)


class SerialReader:
    def __init__(self, config, database, hub):
        self.config = config
        self.terminal_detail = getattr(config, "terminal_detail", False)
        self.database = database
        self.hub = hub
        self.state = GatewayState(
            serial_port="mock" if config.mock_serial else config.serial_port
        )
        self._stop = asyncio.Event()
        self._connection = None
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._fence_lock = asyncio.Lock()
        self._next_request_id = 1
        self._pending_commands: dict[int, asyncio.Future] = {}
        self._fence_result: asyncio.Future | None = None
        self._fence_request_id: int | None = None
        self._fence_collar_id: int | None = None
        self._fence_version: int | None = None
        self.collar_status: dict[int, dict] = {}
        self.gateway_state: dict | None = None
        self.radio_config: dict | None = None
        self._serial_line = bytearray()
        self._discard_until_lf = False
        self._work_queue: asyncio.PriorityQueue[_WorkItem] | None = None
        self._work_sequence = count()
        self._queued_metrics = 0
        self._queued_pdr = 0
        self._refused_metrics = 0
        self._refused_legacy = 0
        self._admitted_work = 0
        self._processed_work = 0
        self._failed_work = 0
        self._legacy_work_error: Exception | None = None
        self._warned_metric_producers: set[str] = set()
        self._metric_acks: asyncio.Queue[tuple[object, MetricSummary]] | None = None
        self._refused_metric_acks = 0
        self._serial_needs_separator = None

    async def run(self):
        if self.config.mock_serial:
            await self._run_mock()
            return
        queue: asyncio.PriorityQueue[_WorkItem] = asyncio.PriorityQueue(maxsize=_WORK_QUEUE_CAPACITY)
        self._work_queue = queue
        self._queued_metrics = self._refused_metrics = 0
        self._queued_pdr = 0
        self._refused_legacy = self._admitted_work = self._processed_work = self._failed_work = 0
        self._legacy_work_error = None
        worker = asyncio.create_task(self._work_loop(queue), name="vfence-serial-storage")
        ack_writer = None
        if getattr(self.config, "metrics_serial_ack_enabled", False):
            self._metric_acks = asyncio.Queue(maxsize=_METRIC_ACK_CAPACITY)
            self._refused_metric_acks = 0
            ack_writer = asyncio.create_task(
                self._write_metric_acks(self._metric_acks), name="vfence-serial-metric-acks",
            )
        try:
            await self._run_serial()
        finally:
            self._work_queue = None
            self._metric_acks = None
            cleanup = asyncio.gather(
                self._finish_work(queue, worker),
                self._finish_metric_acks(ack_writer),
            )
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def stop(self):
        self._stop.set()

    async def _submit_work(self, operation: Callable[[], Awaitable[bool | None]], *,
                           priority: int = _LEGACY_WORK_PRIORITY, description: str = "") -> None:
        queue = self._work_queue
        if queue is None:
            await operation()
            return
        if (queue.full()
                or priority == _METRIC_WORK_PRIORITY and self._queued_metrics >= _METRIC_QUEUE_CAPACITY
                or priority == _PDR_WORK_PRIORITY and self._queued_pdr >= _PDR_QUEUE_CAPACITY):
            if priority != _LEGACY_WORK_PRIORITY:
                self._refused_metrics += 1
                refused, label = self._refused_metrics, "metric summary"
            else:
                self._refused_legacy += 1
                refused, label = self._refused_legacy, "legacy Serial work"
            if refused == 1 or refused % 100 == 0:
                logger.warning("[ALERTA] [BASE] FILA CHEIA | %s queue full (%d refused) %s",
                               label, refused, description)
            return
        queue.put_nowait((priority, next(self._work_sequence), operation))
        self._admitted_work += 1
        if priority == _METRIC_WORK_PRIORITY:
            self._queued_metrics += 1
        elif priority == _PDR_WORK_PRIORITY:
            self._queued_pdr += 1

    async def _work_loop(self, queue: asyncio.PriorityQueue[_WorkItem]) -> None:
        while True:
            priority, _, operation = await queue.get()
            try:
                if operation is None:
                    return
                metric = priority != _LEGACY_WORK_PRIORITY
                if priority == _METRIC_WORK_PRIORITY:
                    self._queued_metrics -= 1
                elif priority == _PDR_WORK_PRIORITY:
                    self._queued_pdr -= 1
                try:
                    if await operation() is False:
                        self._failed_work += 1
                except (KeyError, ValueError, TypeError, RecursionError) as exc:
                    self._failed_work += 1
                    logger.warning("gateway application line rejected (%s)", exc)
                except Exception as exc:
                    self._failed_work += 1
                    logger.exception("gateway Serial work failed (metric=%s)", metric)
                    if not metric and self._legacy_work_error is None:
                        self._legacy_work_error = exc
                finally:
                    self._processed_work += 1
            finally:
                queue.task_done()

    async def _finish_work(self, queue: asyncio.PriorityQueue[_WorkItem], worker: asyncio.Task) -> None:
        discarded = 0
        try:
            await asyncio.wait_for(queue.join(), timeout=_WORK_DRAIN_SECONDS)
        except asyncio.TimeoutError:
            discarded = queue.qsize()
            for _ in range(discarded):
                queue.get_nowait()
                queue.task_done()
            if discarded:
                logger.warning("gateway Serial work discarded on shutdown (%d items, %d metrics)",
                               discarded, self._queued_metrics)
        finally:
            self._queued_metrics = 0
            queue.put_nowait((_LEGACY_WORK_PRIORITY, next(self._work_sequence), None))
            # Uma operacao SQLite em andamento na thread precisa concluir antes de sair.
            await worker
        if self._refused_metrics:
            logger.warning("metric summary queue shutdown (%d total refused)", self._refused_metrics)
        if self._refused_legacy:
            logger.warning("legacy Serial work queue shutdown (%d total refused)", self._refused_legacy)
        logger.info("gateway Serial work totals (admitted=%d, processed=%d, failed=%d, discarded=%d, refused=%d)",
                    self._admitted_work, self._processed_work, self._failed_work, discarded,
                    self._refused_metrics + self._refused_legacy)

    async def _persist_link_event(self, kind: str, timestamp: str, detail: str) -> bool:
        try:
            await asyncio.to_thread(self.database.enqueue_event, kind, timestamp=timestamp, detail=detail)
        except Exception:
            logger.exception("failed to queue %s", kind)
            return False
        return True

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
        opened_before = False
        while not self._stop.is_set():
            connection = None
            try:
                import serial

                logger.info("[USB] [BASE] aguardando Gateway porta=%s", self.config.serial_port)
                connection = await asyncio.to_thread(
                    serial.Serial,
                    self.config.serial_port,
                    self.config.serial_baud,
                    timeout=1,
                )
                # Evita resets causados pelas linhas de controle em algumas
                # placas ESP32. Falhar aqui não impede o uso da Serial.
                try:
                    connection.dtr = False
                    connection.rts = False
                except Exception as exc:
                    logger.debug("could not disable DTR/RTS (%s)", exc)
                self._connection = connection
                self._serial_needs_separator = connection
                self.state.status = "online"
                self.state.error = None
                logger.info("\n%s\n[USB] [BASE] porta %s | porta=%s baud=%s\n%s",
                            RULE, "reaberta" if opened_before else "aberta",
                            self.config.serial_port, self.config.serial_baud, RULE)
                opened_before = True
                if self.config.central_sync_enabled:
                    await self._submit_work(partial(self._persist_link_event, "gateway_online",
                                                   datetime.now(timezone.utc).isoformat(),
                                                   self.config.serial_port))
                await self.hub.broadcast(
                    {"type": "gateway", "data": self.as_dict()}
                )

                while not self._stop.is_set():
                    chunk = await asyncio.to_thread(self._read_serial_chunk, connection)
                    if self._legacy_work_error is not None:
                        failure, self._legacy_work_error = self._legacy_work_error, None
                        raise failure
                    if self._stop.is_set():
                        break
                    await self._process_serial_chunk(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state_changed = self.state.status != "offline" or self.state.error != str(exc)
                if state_changed:
                    logger.warning("[ERRO] [BASE] USB/Gateway indisponivel | porta=%s motivo=%s",
                                   self.config.serial_port, exc)
                    if self.config.central_sync_enabled:
                        await self._submit_work(partial(self._persist_link_event, "gateway_offline",
                                                       datetime.now(timezone.utc).isoformat(), str(exc)[:500]))
                self.state.status = "offline"
                self.state.error = str(exc)
                await self.hub.broadcast(
                    {"type": "gateway", "data": self.as_dict()}
                )
            finally:
                self._connection = None
                self._serial_needs_separator = None
                self._serial_line.clear()
                self._discard_until_lf = False
                self._fail_pending("serial_disconnected")
                if connection is not None:
                    await asyncio.to_thread(connection.close)

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.reconnect_seconds
                )
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _read_serial_chunk(connection) -> bytes:
        return connection.read(max(1, min(connection.in_waiting, _SERIAL_READ_BYTES)))

    async def _process_serial_chunk(self, chunk: bytes) -> None:
        if not chunk:
            # Alguns emissores legados entregam um pacote completo sem LF.
            # Mesmo quando aceito, seu restante fisico ainda pertence a ele.
            if self._serial_line and await self.process_line(bytes(self._serial_line), at_timeout=True):
                self._serial_line.clear()
                self._discard_until_lf = True
            return
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            await self._append_serial_part(part, terminated=True)
        if parts[-1]:
            await self._append_serial_part(parts[-1], terminated=False)

    async def _append_serial_part(self, part: bytes, *, terminated: bool) -> None:
        if self._discard_until_lf:
            self._discard_until_lf = not terminated
            return
        # Reserva um byte para o LF, inclusive quando ainda nao chegou.
        if len(self._serial_line) + len(part) + 1 > _LEGACY_LINE_MAX_BYTES:
            self._serial_line.clear()
            self._discard_until_lf = not terminated
            logger.warning("gateway application line rejected (line exceeds %d bytes)", _LEGACY_LINE_MAX_BYTES)
            return
        self._serial_line.extend(part)
        if terminated:
            self._serial_line.append(10)
            line = bytes(self._serial_line)
            self._serial_line.clear()
            await self.process_line(line)

    async def process_line(self, line, *, at_timeout: bool = False) -> bool:
        try:
            text = line.decode("utf-8").strip() if isinstance(line, bytes) else str(line).strip()
            if not text or "\x00" in text:
                return False
            app_packet = self._parse_gateway_app(text, line)
            if app_packet is not None:
                # Versioned Gateway frames always end with LF. A full JSON body
                # at an idle read can still be a short write awaiting resync.
                if at_timeout and "schema" in app_packet:
                    return False
                if app_packet["kind"] in _FRAMED_SUMMARIES or app_packet["kind"] in _IMMEDIATE_APP_KINDS:
                    await self._process_gateway_app(app_packet)
                else:
                    kind = "POSITION_STATUS" if app_packet["kind"] == "telemetry" else app_packet["kind"]
                    await self._submit_work(partial(self._process_gateway_app, app_packet),
                                            description=f"[{collar_label(app_packet.get('collarId'))}] tipo={kind}")
                return True
            # Linhas humanas do firmware são diagnóstico, não protocolo. Só
            # os dois formatos históricos abaixo ainda entram no parser V1.
            if not (text.startswith("V1|") or text.lower().startswith("recebido:")):
                if not at_timeout:
                    self._diagnostic("[RAW GATEWAY] %s", text)
                return False
            packet = parse_message(line, max_length=self.config.max_line_length)
            if packet is None:
                return False
            await self._submit_work(partial(self._save_and_broadcast, packet),
                                    description=f"[{collar_label(packet.collar_id)}] tipo={packet.type}")
            return True
        except ProtocolError as exc:
            if not at_timeout:
                logger.warning("packet rejected (%s)", exc)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError,
                ValueError, TypeError, RecursionError) as exc:
            if not at_timeout:
                logger.warning("gateway application line rejected (%s)", exc)
        return False

    def _diagnostic(self, message: str, *args) -> None:
        logger.log(logging.INFO if self.terminal_detail else logging.DEBUG, message, *args)

    async def _save_and_broadcast(self, packet: ParsedPacket):
        self._diagnostic("[RAW POS] [BASE] %s", packet.raw_message)
        collar, event = await asyncio.to_thread(
            self.database.save_packet, packet, self.config.central_sync_enabled
        )
        self.state.last_message = packet.raw_message
        self.state.last_seen = datetime.now(timezone.utc).isoformat()
        gps_valid = False if packet.zone == "GNSS_INVALIDO" else (
            True if packet.latitude is not None and packet.longitude is not None else None
        )
        logger.info("%s", position_line("BASE", packet.collar_id, packet.sequence,
                                        gps_valid, packet.hdop, packet.rssi, packet.snr, packet.zone))
        if event:
            previous, current = event["old_value"], event["new_value"]
            if previous and previous != current and current == "FORA":
                logger.warning("\n%s", zone_transition_alert(event["collar_id"], previous, current))
            else:
                log = logger.info if (packet.version != "GATEWAY_V1" or previous == "FORA"
                                      or current in {"ATENCAO", "CRITICO"}) else self._diagnostic
                log("[ZONA BASE] [%s] %s -> %s", collar_label(event["collar_id"]),
                    previous or "N/A", current)
        await self.hub.broadcast({
            "type": "telemetry", "data": packet.as_dict(),
            "collar": collar, "event": event,
        })

    @staticmethod
    def _collar_name(value) -> str:
        return f"COL{int(value):02d}"

    @staticmethod
    def _has_metric_kind(payload: str) -> bool:
        # Procura o kind na raiz sem decodificar valores longos. O parser JSON
        # ainda valida as mensagens legadas que se enquadram no teto maior.
        depth = 0
        for token in _JSON_TOKEN.finditer(payload):
            value = token[0]
            if value in ("{", "["):
                depth += 1
            elif value in ("}", "]"):
                depth -= 1
            elif depth == 1 and len(value) <= 26 and json.decoder.scanstring(value, 1)[0] == "kind":
                match = _JSON_STRING_MEMBER.match(payload, token.end())
                if match and json.decoder.scanstring(match[1], 1)[0] in _FRAMED_SUMMARIES:
                    return True
        return False

    def _parse_gateway_app(self, line: str, raw_line: str | bytes):
        if not line.startswith("[APP] "):
            return None
        wire = raw_line if isinstance(raw_line, bytes) else raw_line.encode("utf-8")
        payload = line[len(_APP_PREFIX):]
        if len(wire) > _LEGACY_LINE_MAX_BYTES:
            raise ValueError(f"linha excede {_LEGACY_LINE_MAX_BYTES} bytes")
        if len(wire) > METRIC_LINE_MAX_BYTES and self._has_metric_kind(payload):
            raise ValueError(f"resumo excede {METRIC_LINE_MAX_BYTES} bytes")
        pairs = []

        def capture_pairs(values):
            # json.loads chama o hook do objeto-raiz depois dos objetos filhos.
            nonlocal pairs
            pairs = values
            return dict(values)

        data = json.loads(payload, object_pairs_hook=capture_pairs)
        if not isinstance(data, dict):
            raise ValueError("mensagem [APP] deve ser um objeto JSON")
        if any(key == "kind" and isinstance(value, str) and value in _FRAMED_SUMMARIES
               for key, value in pairs):
            if not wire.startswith(_APP_PREFIX) or not wire.endswith(b"\n"):
                raise ValueError("resumo exige prefixo [APP] e terminador LF ou CRLF")
            body = wire[:-1].removesuffix(b"\r")
            if b"\n" in body or b"\r" in body:
                raise ValueError("resumo deve ocupar uma unica linha")
            if len(wire) > METRIC_LINE_MAX_BYTES:
                raise ValueError(f"resumo excede {METRIC_LINE_MAX_BYTES} bytes")
            if len(pairs) != len(data):
                raise ValueError("chave JSON duplicada no resumo")
            schema = PDR_SCHEMA if data.get("kind") == PDR_KIND else GATEWAY_SERIAL_SCHEMA
            if data.get("schema") != schema:
                raise ValueError("schema do resumo ausente ou nao suportado")
            return data
        schema = data.get("schema")
        legacy_kind = data.get("type")
        if schema is None:
            if legacy_kind not in {"telemetry", "event", "offline"}:
                raise ValueError("schema [APP] ausente")
            kind = legacy_kind
        elif schema != GATEWAY_SERIAL_SCHEMA:
            raise ValueError("schema [APP] não suportado")
        else:
            kind = data.get("kind")
        if kind not in GATEWAY_APP_KINDS and kind != "offline":
            raise ValueError("kind [APP] desconhecido")
        data["kind"] = kind
        return data

    def _telemetry_packet(self, data: dict, line: str) -> ParsedPacket:
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

    async def _process_gateway_app(self, data: dict) -> None:
        kind = data["kind"]
        if self.terminal_detail and kind != "telemetry":
            self._diagnostic("[RAW GATEWAY] %s", json.dumps(data, separators=(",", ":")))
        if kind in METRIC_SUMMARY_KINDS:
            await self._process_metric_summary(data)
            return
        if kind == PDR_KIND:
            summary = parse_pdr_summary(data)
            gateway_id = getattr(self.config, "metrics_gateway_id", "")
            if not gateway_id:
                if "pdr" not in self._warned_metric_producers:
                    logger.warning("PDR ignorado (METRICS_GATEWAY_ID nao configurado)")
                    self._warned_metric_producers.add("pdr")
                return
            await self._submit_work(partial(self._persist_pdr_summary, gateway_id, summary),
                                    priority=_PDR_WORK_PRIORITY,
                                    description=f"[{collar_label(summary.collar_id)}] tipo=PDR")
            return
        if kind == "telemetry":
            await self._save_and_broadcast(
                self._telemetry_packet(
                    data,
                    "[APP] " + json.dumps(data, separators=(",", ":")),
                )
            )
            return

        if kind == "command_response":
            request_id = data.get("requestId")
            future = self._pending_commands.get(request_id)
            if future is not None and not future.done():
                future.set_result(data)
            if not data.get("ok") or data.get("command") != "FENCE_POINT" or self.terminal_detail:
                log = logger.info if data.get("ok") else logger.warning
                log("[%s] [BASE] resposta Gateway comando=%s requestId=%s detalhe=%s",
                    "OK" if data.get("ok") else "ERRO", value_text(data.get("command")), value_text(request_id),
                    value_text(data.get("detail")))
            return

        if kind == "fence_transfer":
            self._handle_fence_transfer(data)
            state = value_text(data.get("state"))
            log = logger.warning if state == "failed" else logger.info
            log("\n%s\n[CERCA] [BASE] [%s] versao=%s requestId=%s estadoGateway=%s detalhe=%s\n%s",
                RULE, collar_label(data.get("collarId")), value_text(data.get("fenceVersion")),
                value_text(data.get("requestId")), state, value_text(data.get("detail")), RULE)
            await self.hub.broadcast({"type": "fence_transfer", "data": data})
            return

        if kind == "collar_status" and not data.get("empty"):
            collar_id = data.get("collarId")
            if isinstance(collar_id, int):
                self.collar_status[collar_id] = dict(data)
        elif kind == "collar_state":
            collar_id = data.get("collarId")
            if isinstance(collar_id, int):
                previous = self.collar_status.get(collar_id, {})
                self.collar_status[collar_id] = {**previous, **data}
        elif kind == "gateway_state":
            self.gateway_state = dict(data)
        elif kind == "radio_config":
            self.radio_config = dict(data)

        await self._process_gateway_event(data)

    async def _process_metric_summary(self, data: dict) -> None:
        setting = "metrics_installation_id" if data["kind"] == HDOP_SUMMARY_KIND else "metrics_gateway_id"
        producer_id = getattr(self.config, setting, "")
        summary = parse_metric_summary(data, producer_id)
        if not producer_id:
            if setting not in self._warned_metric_producers:
                logger.warning("metric summary ignored (%s not configured)", setting.upper())
                self._warned_metric_producers.add(setting)
            return
        connection = self._connection if self._metric_acks is not None else None
        await self._submit_work(partial(self._persist_metric_summary, summary, connection),
                                priority=_METRIC_WORK_PRIORITY, description=metric_identity(summary))

    async def _persist_pdr_summary(self, gateway_id: str, summary: PdrSummary) -> bool:
        result = await asyncio.to_thread(self.database.save_pdr_summary, gateway_id, summary)
        metric = f"{summary.pdr_x100 / 100:.2f}%" if summary.valid else "N/A"
        log = logger.info if result in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE) else logger.warning
        log("[PDR 60s] [BASE] [%s] sessao=%s janela=%s rx=%u PDR estimado=%s motivo=%s SQLite=%s",
            collar_label(summary.collar_id), summary.gateway_session_id, summary.window_id,
            summary.received_count, metric, summary.reason, result.value)
        return result in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE)

    async def _persist_metric_summary(self, summary: MetricSummary, connection=None) -> bool:
        result = await asyncio.to_thread(self.database.save_metric_summary, summary)
        identity = metric_identity(summary)
        if result is MetricSaveResult.INSERTED:
            logger.info("\n%s", metric_block("BASE", summary, "[SQLITE] INSERTED | COMMIT concluido"))
        elif result is MetricSaveResult.DUPLICATE:
            logger.info("[SQLITE] [BASE] %s DUPLICATE | registro ja persistido", identity)
        elif result is MetricSaveResult.CONFLICT:
            logger.warning("[ERRO] [SQLITE] [BASE] %s CONFLICT | identidade igual, conteudo diferente", identity)
        else:
            logger.warning("[ERRO] [SQLITE] [BASE] %s FAILED | confirmacao nao autorizada", identity)
        if result not in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE):
            return False
        queue = self._metric_acks
        if (queue is not None and connection is not None and connection is self._connection
                and not self._stop.is_set()
                and (summary.kind == "RADIO" or summary.gateway_session_id is not None)):
            try:
                queue.put_nowait((connection, summary))
                logger.info("[ACK FILA] [BASE] %s confirmacao enfileirada", identity)
            except asyncio.QueueFull:
                self._refused_metric_acks += 1
                if self._refused_metric_acks == 1 or self._refused_metric_acks % 100 == 0:
                    logger.warning("[AVISO] [BASE] %s ACK NAO ENFILEIRADO | fila cheia (%d recusados)",
                                   identity, self._refused_metric_acks)
        return True

    async def _write_metric_acks(self, queue: asyncio.Queue[tuple[object, MetricSummary]]) -> None:
        while True:
            connection, summary = await queue.get()
            try:
                await self._send_metric_ack(connection, summary)
            except Exception as exc:
                logger.warning("[ERRO] [ACK SERIAL] [BASE] %s falha de escrita (%s)", metric_identity(summary), exc)

    async def _finish_metric_acks(self, writer: asyncio.Task | None) -> None:
        if writer is not None:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer

    async def _send_metric_ack(self, connection, summary: MetricSummary) -> None:
        hdop = summary.kind == "HDOP"
        message = {
            "schema": GATEWAY_SERIAL_SCHEMA,
            "requestId": await self.next_request_id(),
            "command": "METRIC_COMMITTED",
            "kind": HDOP_SUMMARY_KIND if hdop else RADIO_SUMMARY_KIND,
            "collarId": summary.collar_id,
            "sessionId": summary.session_id,
            "windowId": summary.window_id,
            "gatewaySessionId": summary.gateway_session_id if hdop else summary.session_id,
        }
        line = b"[CMD] " + json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n"
        async with self._write_lock:
            if (self._metric_acks is None or self._stop.is_set() or connection is not self._connection
                    or not getattr(connection, "is_open", True)):
                return
            if self._serial_needs_separator is connection:
                line = _SERIAL_RESYNC + line
            self._serial_needs_separator = connection
            write = asyncio.create_task(asyncio.to_thread(self._write_metric_ack_line, connection, line))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # Segure o lock ate a escrita fisica ja iniciada terminar.
                with suppress(Exception):
                    await write
                raise
            if self._serial_needs_separator is connection:
                self._serial_needs_separator = None
            logger.info("[ACK SERIAL] [BASE] %s escrita local aceita requestId=%d",
                        metric_identity(summary), message["requestId"])

    @staticmethod
    def _write_metric_ack_line(connection, line: bytes) -> None:
        previous_timeout = connection.write_timeout
        try:
            connection.write_timeout = _METRIC_ACK_WRITE_SECONDS
            if connection.write(line) != len(line):
                raise OSError("incomplete metric confirmation")
        finally:
            connection.write_timeout = previous_timeout

    async def _process_gateway_event(self, data: dict):
        timestamp = datetime.now(timezone.utc).isoformat()
        collar_id = self._collar_name(data["collarId"]) if isinstance(data.get("collarId"), int) else None
        kind = data.get("kind")
        should_enqueue = kind in {"event", "collar_state", "offline"}
        if kind == "collar_state":
            event_kind = f"collar_{str(data.get('state', 'unknown')).lower()}"
        else:
            event_kind = str(data.get("subtype") or data.get("event") or kind or "gateway_event").lower()
        if should_enqueue:
            name = str(data.get("state") or kind).upper() if kind in {"collar_state", "offline"} else event_kind.upper()
            details = f"seq={value_text(data.get('sequence'))} cerca={value_text(data.get('fenceVersion'))}" if kind == "event" else ""
            log = logger.warning if name in {"OUTSIDE", "GPS_LOST", "LOW_BATTERY", "CRITICAL_BATTERY", "OFFLINE"} else logger.info
            log("\n%s", event_block("BASE / GATEWAY", collar_id, name, details))
        if self.config.central_sync_enabled and should_enqueue:
            await asyncio.to_thread(
                self.database.enqueue_event,
                event_kind,
                timestamp=timestamp,
                collar_id=collar_id,
                detail=json.dumps(data, separators=(",", ":")),
            )
        await self.hub.broadcast({"type": "gateway_event", "data": data})

    def _handle_fence_transfer(self, data: dict) -> None:
        if not self._fence_result or self._fence_result.done():
            return
        if data.get("requestId") != self._fence_request_id:
            return
        if data.get("collarId") != self._fence_collar_id:
            return
        if data.get("fenceVersion") != self._fence_version:
            return
        if data.get("state") == "confirmed":
            self._fence_result.set_result((True, "collar_confirmed"))
        elif data.get("state") == "failed":
            self._fence_result.set_result((False, str(data.get("detail") or "failed")))

    def _fail_pending(self, detail: str) -> None:
        for future in self._pending_commands.values():
            if not future.done():
                future.set_exception(ConnectionError(detail))
        self._pending_commands.clear()
        if self._fence_result and not self._fence_result.done():
            self._fence_result.set_result((False, detail))

    async def next_request_id(self) -> int:
        async with self._request_lock:
            while True:
                request_id = self._next_request_id
                self._next_request_id = 1 if request_id == 0xFFFFFFFF else request_id + 1
                if request_id not in self._pending_commands:
                    return request_id

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
            line = (command + "\n").encode("utf-8")
            # NUL invalida a linha anterior e LF fecha o descarte no Gateway.
            if self._serial_needs_separator is connection:
                line = _SERIAL_RESYNC + line
            self._serial_needs_separator = connection
            if await asyncio.to_thread(connection.write, line) != len(line):
                raise OSError("incomplete Serial command")
            if self._serial_needs_separator is connection:
                self._serial_needs_separator = None
            await asyncio.to_thread(connection.flush)

    async def send_gateway_command(
        self,
        command: str,
        *,
        request_id: int | None = None,
        timeout_seconds: float = 5.0,
        **fields,
    ) -> dict:
        if request_id is None:
            request_id = await self.next_request_id()
        if (not isinstance(request_id, int) or isinstance(request_id, bool)
                or not 1 <= request_id <= 0xFFFFFFFF):
            raise ValueError("requestId deve estar entre 1 e 4294967295")
        message = {
            "schema": GATEWAY_SERIAL_SCHEMA,
            "requestId": request_id,
            "command": command,
            **fields,
        }
        if self.config.mock_serial:
            logger.info("mock gateway command (%s requestId=%s)", command, request_id)
            return {**message, "kind": "command_response", "ok": True}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_commands[request_id] = future
        try:
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True)
            await self.send_command(f"[CMD] {payload}")
            response = await asyncio.wait_for(future, timeout=timeout_seconds)
            return response
        finally:
            self._pending_commands.pop(request_id, None)

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
            self._fence_collar_id = numeric_id
            self._fence_version = int(fence["version"])
            try:
                critical_m = max(0, round(int(fence["margin_critical_cm"]) / 100))
                attention_m = max(critical_m, round(int(fence["margin_attention_cm"]) / 100))
                response = await self.send_gateway_command(
                    "SET_PARAMETERS",
                    collarId=numeric_id,
                    veryNearM=critical_m,
                    nearM=attention_m,
                    telemetryIntervalS=self.config.collar_telemetry_seconds,
                    gpsTimeoutS=self.config.collar_gps_timeout_seconds,
                    lowBatteryPct=self.config.collar_low_battery_pct,
                    criticalBatteryPct=self.config.collar_critical_battery_pct,
                )
                if not response.get("ok"):
                    return False, str(response.get("detail") or "set_parameters_rejected")
                points = fence["points_e6"]
                response = await self.send_gateway_command(
                    "FENCE_NEW", collarId=numeric_id,
                    fenceVersion=int(fence["version"]), vertexCount=len(points),
                )
                if not response.get("ok"):
                    return False, str(response.get("detail") or "fence_new_rejected")
                for index, (latitude_e6, longitude_e6) in enumerate(points):
                    response = await self.send_gateway_command(
                        "FENCE_POINT", index=index,
                        latitude=round(latitude_e6 / 1_000_000, 7),
                        longitude=round(longitude_e6 / 1_000_000, 7),
                    )
                    if not response.get("ok"):
                        return False, str(response.get("detail") or "fence_point_rejected")
                self._fence_request_id = await self.next_request_id()
                response = await self.send_gateway_command(
                    "FENCE_SEND", request_id=self._fence_request_id,
                )
                if not response.get("ok"):
                    return False, str(response.get("detail") or "fence_send_rejected")
                return await asyncio.wait_for(self._fence_result, timeout=timeout_seconds)
            except (asyncio.TimeoutError, ConnectionError):
                return False, "gateway_confirmation_timeout"
            finally:
                self._fence_result = None
                self._fence_request_id = None
                self._fence_collar_id = None
                self._fence_version = None

    def as_dict(self):
        return {
            "status": self.state.status,
            "serial_port": self.state.serial_port,
            "last_message": self.state.last_message,
            "last_seen": self.state.last_seen,
            "error": self.state.error,
            "mock": self.config.mock_serial,
        }
