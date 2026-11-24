from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import partial
from heapq import heapify
from itertools import count, cycle

from app.database import GatewayEventConflictError, MetricSaveResult
from app.metric_protocol import (
    HDOP_SUMMARY_KIND,
    METRIC_LINE_MAX_BYTES,
    METRIC_SUMMARY_KINDS,
    RADIO_SUMMARY_KIND,
    MetricSummary,
    parse_metric_summary,
)
from app.models import GatewayState
from app.pdr_protocol import PDR_COMMIT_SCHEMA, PDR_KIND, PDR_SCHEMA, PdrSummary, parse_pdr_summary
from app.protocol import ParsedPacket, ProtocolError, parse_message
from app.terminal import (
    ALERT_EVENTS, base_block, base_event, base_metric, base_pdr, base_position, base_value,
    collar_label, fpga_metric_diagnostic, gateway_log_field, gateway_log_snapshot, metric_identity,
)

logger = logging.getLogger("serial_reader")

GATEWAY_SERIAL_SCHEMA = "vfence.gateway.serial.v1"
_APP_PREFIX = b"[APP] "
_SERIAL_READ_BYTES = 256
# Deixa margem para respostas que incluem texto de comandos de ate 512 caracteres.
_LEGACY_LINE_MAX_BYTES = 4096
_WORK_QUEUE_CAPACITY = 128
# O volume de trabalho comum nao consome a reserva de alertas da coleira.
_CRITICAL_EVENT_CAPACITY = 16
_METRIC_QUEUE_CAPACITY = 64
_PDR_QUEUE_CAPACITY = 8
# A rajada PDR pode ocupar ate oito vagas. Preserve o mesmo espaco para
# trabalho de maior prioridade, sem reduzir a capacidade das filas existentes.
_PDR_OPERATIONAL_RESERVE = _PDR_QUEUE_CAPACITY
_WORK_DRAIN_SECONDS = 2.0
_CRITICAL_EVENT_PRIORITY = -1
_LEGACY_WORK_PRIORITY = 0
_METRIC_WORK_PRIORITY = 1
_PDR_WORK_PRIORITY = 2
_PRIORITY_EVENT_NAMES = {
    "OUTSIDE", "BACK_INSIDE", "GPS_LOST", "GPS_RECOVERED", "LOW_BATTERY", "CRITICAL_BATTERY",
}
_GATEWAY_EVENT_KINDS = {"event", "collar_state", "offline"}
_EVENT_SQLITE_RETRY_SECONDS = (0.05, 0.2)
_METRIC_ACK_CAPACITY = 16
_METRIC_ACK_WRITE_SECONDS = 0.25
_GATEWAY_METRIC_ACK_LOG = re.compile(
    r"\[ACK SQLITE\] \[GATEWAY\] \[COL [0-9]{3}\] tipo=(?:HDOP|RADIO) "
    r"sessao=[0-9a-f]{16} janela=[0-9a-f]{16} identidade validada, pendencia consumida"
)
_SERIAL_RESYNC = b"\x00\n"
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_JSON_TOKEN = re.compile(_JSON_STRING + r"|[\[\]{}]")
_JSON_STRING_MEMBER = re.compile(r"\s*:\s*(" + _JSON_STRING + ")")
_IMMEDIATE_APP_KINDS = {"command_response", "fence_transfer"}
_FRAMED_SUMMARIES = METRIC_SUMMARY_KINDS | {PDR_KIND}
_SAFE_FRAME_REJECTIONS = {
    "mensagem [APP] deve ser um objeto JSON",
    "resumo exige prefixo [APP] e terminador LF ou CRLF",
    "resumo deve ocupar uma unica linha",
    "chave JSON duplicada no resumo",
    "schema do resumo ausente ou nao suportado",
    "schema [APP] ausente",
    "schema [APP] não suportado",
    "kind [APP] desconhecido",
}
_WorkItem = tuple[int, int, Callable[[], Awaitable[bool | None]] | None]


class _LineResult(Enum):
    UNRECOGNIZED = 0
    REFUSED = 1
    ACCEPTED = 2


class _SerialWorkQueue(asyncio.PriorityQueue[_WorkItem]):
    def __init__(self, maxsize: int):
        super().__init__(maxsize=maxsize + _CRITICAL_EVENT_CAPACITY)
        self.regular_capacity = maxsize

    def evict_newest_pdr(self) -> bool:
        # O heap pertence ao event loop. Remover e confirmar a pendencia sem
        # await preserva a ordem dos demais itens e a contagem usada por join().
        newest = max(
            (index for index, item in enumerate(self._queue) if item[0] == _PDR_WORK_PRIORITY),
            key=lambda index: self._queue[index][1],
            default=None,
        )
        if newest is None:
            return False
        self._queue.pop(newest)
        heapify(self._queue)
        self.task_done()
        return True

    def discard_regular(self) -> int:
        preserved = [item for item in self._queue if item[0] == _CRITICAL_EVENT_PRIORITY]
        discarded = len(self._queue) - len(preserved)
        self._queue[:] = preserved
        heapify(self._queue)
        for _ in range(discarded):
            self.task_done()
        return discarded


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
        self.collar_contacts: dict[str, dict] = {}
        self.collar_gnss: dict[str, dict] = {}
        self.last_response_at: str | None = None
        self.connection_error_kind: str | None = None
        self.gateway_state: dict | None = None
        self.radio_config: dict | None = None
        self.effective_profile: dict | None = None
        self._serial_line = bytearray()
        self._discard_until_lf = False
        self._work_queue: _SerialWorkQueue | None = None
        self._work_sequence = count()
        self._queued_metrics = 0
        self._queued_pdr = 0
        self._queued_critical = 0
        self._refused_metrics = 0
        self._refused_pdr = 0
        self._evicted_pdr = 0
        self._refused_legacy = 0
        self._refused_critical = 0
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
        queue = _SerialWorkQueue(maxsize=_WORK_QUEUE_CAPACITY)
        self._work_queue = queue
        self._queued_metrics = self._refused_metrics = 0
        self._queued_pdr = self._refused_pdr = self._evicted_pdr = 0
        self._queued_critical = self._refused_critical = 0
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
                           priority: int = _LEGACY_WORK_PRIORITY, description: str = "") -> bool:
        queue = self._work_queue
        if queue is None:
            return await operation() is not False
        if priority == _CRITICAL_EVENT_PRIORITY:
            if self._queued_critical == _CRITICAL_EVENT_CAPACITY:
                self._refused_critical += 1
                logger.error("%s", base_block(
                    "ERRO | FILA SERIAL", f"Evento: {description}",
                    f"Estado: RECUSADO POR SATURACAO | Criticos recusados: {self._refused_critical}",
                    critical=True,
                ))
                return False
            queue.put_nowait((priority, next(self._work_sequence), operation))
            self._queued_critical += 1
            self._admitted_work += 1
            self._diagnostic("[BASE] FILA SERIAL | ADMITIDO %s", description)
            return True
        regular_size = queue.qsize() - self._queued_critical
        regular_full = regular_size >= queue.regular_capacity
        metric_limit = priority == _METRIC_WORK_PRIORITY and self._queued_metrics >= _METRIC_QUEUE_CAPACITY
        pdr_limit = priority == _PDR_WORK_PRIORITY and self._queued_pdr >= _PDR_QUEUE_CAPACITY
        reserved = priority == _PDR_WORK_PRIORITY and regular_size >= queue.regular_capacity - _PDR_OPERATIONAL_RESERVE
        if regular_full and priority < _PDR_WORK_PRIORITY and not metric_limit and self._queued_pdr:
            if queue.evict_newest_pdr():
                regular_full = False
                self._queued_pdr -= 1
                self._evicted_pdr += 1
                if self._evicted_pdr == 1 or self._evicted_pdr % 100 == 0:
                    logger.warning("[BASE] AVISO | FILA SERIAL | PDR desalojado (%d) %s",
                                   self._evicted_pdr, description)
        if regular_full or metric_limit or pdr_limit or reserved:
            if priority == _PDR_WORK_PRIORITY:
                self._refused_pdr += 1
                reason = "global capacity" if regular_full else "PDR quota" if pdr_limit else "operational reserve"
                if self._refused_pdr == 1 or self._refused_pdr % 100 == 0:
                    logger.warning("[BASE] AVISO | FILA CHEIA | PDR recusado (%s, %d total) %s",
                                   reason, self._refused_pdr, description)
                return False
            if priority == _METRIC_WORK_PRIORITY:
                self._refused_metrics += 1
                refused, label = self._refused_metrics, "metric summary"
            else:
                self._refused_legacy += 1
                refused, label = self._refused_legacy, "legacy Serial work"
            if refused == 1 or refused % 100 == 0:
                logger.warning("[BASE] AVISO | FILA CHEIA | %s queue full (%d refused) %s",
                               label, refused, description)
            return False
        queue.put_nowait((priority, next(self._work_sequence), operation))
        self._admitted_work += 1
        if priority == _METRIC_WORK_PRIORITY:
            self._queued_metrics += 1
        elif priority == _PDR_WORK_PRIORITY:
            self._queued_pdr += 1
        return True

    async def _work_loop(self, queue: asyncio.PriorityQueue[_WorkItem]) -> None:
        while True:
            priority, _, operation = await queue.get()
            try:
                if operation is None:
                    return
                metric = priority in {_METRIC_WORK_PRIORITY, _PDR_WORK_PRIORITY}
                if priority == _CRITICAL_EVENT_PRIORITY:
                    self._queued_critical -= 1
                elif priority == _METRIC_WORK_PRIORITY:
                    self._queued_metrics -= 1
                elif priority == _PDR_WORK_PRIORITY:
                    self._queued_pdr -= 1
                try:
                    if await operation() is False:
                        self._failed_work += 1
                except (KeyError, ValueError, TypeError, RecursionError) as exc:
                    self._failed_work += 1
                    logger.warning("gateway application line rejected (%s)", type(exc).__name__)
                except Exception as exc:
                    self._failed_work += 1
                    logger.error("%s", base_block(
                        "ERRO | PROCESSAMENTO SERIAL",
                        f"Tipo: {'METRICA' if metric else 'OPERACIONAL'} | Motivo: {base_value(exc)}",
                        critical=True,
                    ), exc_info=self.terminal_detail)
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
            discarded = queue.discard_regular()
            if discarded:
                logger.warning("gateway Serial work discarded on shutdown (%d items, %d metrics)",
                               discarded, self._queued_metrics)
        finally:
            self._queued_metrics = 0
            self._queued_pdr = 0
            queue.put_nowait((_LEGACY_WORK_PRIORITY, next(self._work_sequence), None))
            # Uma operacao SQLite em andamento na thread precisa concluir antes de sair.
            await worker
        if self._refused_metrics:
            logger.warning("metric summary queue shutdown (%d total refused)", self._refused_metrics)
        if self._refused_legacy:
            logger.warning("legacy Serial work queue shutdown (%d total refused)", self._refused_legacy)
        if self._refused_pdr or self._evicted_pdr:
            logger.warning("PDR summary queue shutdown (%d refused, %d displaced)",
                           self._refused_pdr, self._evicted_pdr)
        logger.info("gateway Serial work totals (admitted=%d, processed=%d, failed=%d, discarded=%d, refused=%d)",
                    self._admitted_work, self._processed_work, self._failed_work, discarded + self._evicted_pdr,
                    self._refused_metrics + self._refused_legacy + self._refused_pdr + self._refused_critical)

    async def _persist_link_event(self, kind: str, timestamp: str, detail: str) -> bool:
        try:
            await asyncio.to_thread(self.database.enqueue_event, kind, timestamp=timestamp, detail=detail)
        except Exception as exc:
            logger.error("%s", base_block(
                "ERRO | SQLITE", f"Evento de link: {kind} | Motivo: {base_value(exc)}", critical=True,
            ), exc_info=self.terminal_detail)
            return False
        return True

    async def _run_mock(self):
        logger.info("%s", base_block("GATEWAY | SIMULACAO", "Estado: MOCK INICIADO"))
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

                if not opened_before and self.state.error is None:
                    logger.info("%s", base_block("USB / GATEWAY", f"Porta configurada: {self.config.serial_port}",
                                                 "Estado: AGUARDANDO ABERTURA"))
                elif self.terminal_detail:
                    logger.info("[BASE] USB | tentativa de abertura porta=%s",
                                base_value(self.config.serial_port))
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
                    self._diagnostic("[BASE] USB | falha ao desativar DTR/RTS: %s", type(exc).__name__)
                self._connection = connection
                self.last_response_at = None
                self.collar_contacts.clear()
                self.effective_profile = None
                self._serial_needs_separator = connection
                self.state.status = "online"
                self.state.error = None
                self.connection_error_kind = None
                logger.info("%s", base_block(
                    "USB / GATEWAY",
                    f"Porta: {self.config.serial_port} | Baud: {self.config.serial_baud}",
                    f"Estado: {'PORTA REABERTA' if opened_before else 'PORTA ABERTA'}",
                ))
                opened_before = True
                if self.config.central_sync_enabled:
                    await self._submit_work(partial(self._persist_link_event, "gateway_online",
                                                   datetime.now(timezone.utc).isoformat(),
                                                   self.config.serial_port))
                await self.hub.broadcast(
                    {"type": "gateway", "data": self.as_dict()}
                )
                if getattr(self.config, "event_serial_commit_enabled", False):
                    await self._send_serial_commit_command(connection, "ENABLE_EVENT_COMMIT", deliveryVersion=1)
                if getattr(self.config, "pdr_serial_commit_enabled", False) and getattr(self.config, "metrics_gateway_id", ""):
                    await self._send_serial_commit_command(connection, "ENABLE_PDR_COMMIT", deliveryVersion=2)

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
                    if isinstance(exc, sqlite3.Error):
                        title, status = "ERRO | SQLITE", "LEITURA INTERROMPIDA POR FALHA NO SQLITE"
                    else:
                        title = "ERRO | USB / GATEWAY"
                        status = "CONEXAO SERIAL INTERROMPIDA" if connection is not None else "FALHA DE ABERTURA"
                    logger.warning("%s", base_block(
                        title, f"Porta: {self.config.serial_port} | Estado: {status}",
                        f"Motivo: {base_value(exc)}", critical=True,
                    ))
                    if self.config.central_sync_enabled:
                        await self._submit_work(partial(self._persist_link_event, "gateway_offline",
                                                       datetime.now(timezone.utc).isoformat(), str(exc)[:500]))
                self.state.status = "offline"
                self.state.error = str(exc)
                self.connection_error_kind = "storage" if isinstance(exc, sqlite3.Error) else "serial"
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
            # Um pacote reconhecido (admitido ou recusado) ja foi consumido.
            if not self._serial_line:
                return
            result = await self._handle_line(bytes(self._serial_line), at_timeout=True)
            if result is not _LineResult.UNRECOGNIZED:
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
        """Return admission for queued work, or the handling result for inline work."""
        return await self._handle_line(line, at_timeout=at_timeout) is _LineResult.ACCEPTED

    async def _handle_line(self, line, *, at_timeout: bool) -> _LineResult:
        try:
            text = line.decode("utf-8").strip() if isinstance(line, bytes) else str(line).strip()
            if not text or "\x00" in text:
                return _LineResult.UNRECOGNIZED
            app_packet = self._parse_gateway_app(text, line)
            if app_packet is not None:
                # Versioned Gateway frames always end with LF. A full JSON body
                # at an idle read can still be a short write awaiting resync.
                if at_timeout and "schema" in app_packet:
                    return _LineResult.UNRECOGNIZED
                if "deliveryId" in app_packet or "deliveryVersion" in app_packet:
                    delivery_id = app_packet.get("deliveryId")
                    if (app_packet["kind"] != "event" or type(app_packet.get("deliveryVersion")) is not int or
                            app_packet["deliveryVersion"] != 1 or not isinstance(delivery_id, str) or
                            not re.fullmatch(r"[0-9a-f]{24}", delivery_id) or
                            type(app_packet.get("collarId")) is not int or not 1 <= app_packet["collarId"] <= 254):
                        raise ValueError("invalid event delivery identity")
                if app_packet["kind"] in _FRAMED_SUMMARIES or app_packet["kind"] in _IMMEDIATE_APP_KINDS:
                    admitted = await self._process_gateway_app(app_packet)
                else:
                    priority = _LEGACY_WORK_PRIORITY
                    kind = "POSITION_STATUS" if app_packet["kind"] == "telemetry" else app_packet["kind"]
                    if kind == "event":
                        name = app_packet.get("subtype") or app_packet.get("event")
                        if isinstance(name, str) and name.upper() in _PRIORITY_EVENT_NAMES:
                            priority = _CRITICAL_EVENT_PRIORITY
                            kind = name.upper()
                    admitted = await self._submit_work(
                        partial(self._process_gateway_app, app_packet, datetime.now(timezone.utc)), priority=priority,
                        description=f"[{collar_label(app_packet.get('collarId'))}] tipo={kind}",
                    )
            else:
                # Linhas humanas são diagnóstico. Só os formatos históricos entram no parser V1.
                if not (text.startswith("V1|") or text.lower().startswith("recebido:")):
                    if not at_timeout:
                        if _GATEWAY_METRIC_ACK_LOG.fullmatch(text):
                            logger.info("%s", text)
                        else:
                            self._diagnostic("[RAW GATEWAY] Linha de diagnostico | Caracteres: %s", len(text))
                    return _LineResult.UNRECOGNIZED
                packet = parse_message(line, max_length=self.config.max_line_length)
                if packet is None:
                    return _LineResult.UNRECOGNIZED
                admitted = await self._submit_work(
                    partial(self._save_and_broadcast, packet),
                    description=f"[{collar_label(packet.collar_id)}] tipo={packet.type}",
                )
            if admitted and app_packet is not None and app_packet["kind"] != "telemetry":
                self.last_response_at = datetime.now(timezone.utc).isoformat()
            return _LineResult.ACCEPTED if admitted else _LineResult.REFUSED
        except ProtocolError as exc:
            if not at_timeout:
                logger.warning("packet rejected (%s)", exc)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError,
                ValueError, TypeError, RecursionError) as exc:
            if not at_timeout:
                reason = exc.args[0] if exc.args else None
                label = reason if type(reason) is str and reason in _SAFE_FRAME_REJECTIONS else type(exc).__name__
                logger.warning("gateway application line rejected (%s)", label)
        return _LineResult.UNRECOGNIZED

    def _diagnostic(self, message: str, value: object) -> None:
        if self.terminal_detail:
            logger.info(message, base_value(value, limit=512))

    async def _save_and_broadcast(self, packet: ParsedPacket):
        if self.terminal_detail:
            snapshot = gateway_log_snapshot({
                "sequence": packet.sequence, "latitude": packet.latitude, "longitude": packet.longitude,
                "zone": packet.zone, "satellites": packet.satellites, "hdop": packet.hdop,
                "rssi": packet.rssi, "snr": packet.snr,
            })
            summary = json.dumps(snapshot, separators=(",", ":"))
            self._diagnostic("[RAW POS] [BASE] %s", f"[{collar_label(packet.collar_id)}] {summary}")
        collar, event = await asyncio.to_thread(
            self.database.save_packet, packet, self.config.central_sync_enabled
        )
        self.state.last_message = packet.raw_message
        self.state.last_seen = datetime.now(timezone.utc).isoformat()
        self.last_response_at = self.state.last_seen
        if packet.type == "POS":
            observed = packet.timestamp.isoformat()
            previous = self.collar_contacts.get(packet.collar_id, {})
            if observed >= previous.get("observed_at", ""):
                self.collar_contacts[packet.collar_id] = {
                    "state": "online", "last_contact_at": observed, "observed_at": observed,
                }
        gps_valid = False if packet.zone == "GNSS_INVALIDO" else (
            True if packet.latitude is not None and packet.longitude is not None else None
        )
        logger.info("%s", base_position(
            packet.collar_id, packet.sequence, gps_valid, packet.hdop, packet.rssi, packet.snr,
            packet.zone, detail=self.terminal_detail,
        ))
        if event:
            previous, current = event["old_value"], event["new_value"]
            critical = bool(previous and previous != current and current == "FORA")
            if (critical or packet.version != "GATEWAY_V1" or previous == "FORA"
                    or current in {"ATENCAO", "CRITICO"} or self.terminal_detail):
                log = logger.warning if critical else logger.info
                log("%s", base_block(
                    "ALERTA | TRANSICAO DE ZONA" if critical else "TRANSICAO DE ZONA",
                    f"{base_value(previous)} -> {base_value(current)}",
                    "Origem: telemetria persistida | SQLite: SALVA",
                    collar_id=event["collar_id"], critical=critical,
                ))
        await self.hub.broadcast({
            "type": "telemetry", "data": packet.as_dict(),
            "collar": self.collar_snapshot(collar), "event": event, "gateway": self.as_dict(),
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
        if "deliveryId" in data or "deliveryVersion" in data:
            body = wire.removesuffix(b"\n").removesuffix(b"\r")
            if (data.get("schema") != GATEWAY_SERIAL_SCHEMA or len(pairs) != len(data) or
                    not wire.endswith(b"\n") or len(wire) > 512 or b"\r" in body or b"\n" in body):
                raise ValueError("invalid event delivery frame")
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
            schemas = (PDR_SCHEMA, PDR_COMMIT_SCHEMA) if data.get("kind") == PDR_KIND else (GATEWAY_SERIAL_SCHEMA,)
            if data.get("schema") not in schemas:
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

    def _telemetry_packet(self, data: dict, line: str, received_at: datetime | None = None) -> ParsedPacket:
        collar_id, sequence = data["collarId"], data["sequence"]
        if type(collar_id) is not int or not 1 <= collar_id <= 254:
            raise ValueError("invalid telemetry collar ID")
        if type(sequence) is not int or not 0 <= sequence <= 65535:
            raise ValueError("invalid telemetry sequence")
        for field in ("gpsValid", "inside", "near", "veryNear"):
            if field in data and type(data[field]) is not bool:
                raise ValueError("invalid telemetry flag")

        def number(field, minimum, maximum, *, integer=False):
            value = data.get(field)
            types = (int,) if integer else (int, float)
            if value is not None and (type(value) not in types or not minimum <= value <= maximum):
                raise ValueError("invalid telemetry measurement")
            return value

        latitude = number("latitude", -90, 90)
        longitude = number("longitude", -180, 180)
        gps_valid = data.get("gpsValid") is True
        inside = data.get("inside") is True
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
            collar_id=self._collar_name(collar_id),
            type="POS",
            timestamp=received_at or datetime.now(timezone.utc),
            raw_message=line,
            latitude=latitude if gps_valid else None,
            longitude=longitude if gps_valid else None,
            zone=zone,
            satellites=number("satellites", 0, 254, integer=True) if gps_valid else None,
            hdop=number("hdop", 0, 655.34) if gps_valid else None,
            rssi=number("rssi", -1e6, 1e6),
            snr=number("snr", -1e6, 1e6),
            sequence=sequence,
            battery_pct=number("battery", 0, 100, integer=True),
            fence_version=number("fenceVersion", 0, 65535, integer=True),
        )

    async def _process_gateway_app(self, data: dict, received_at: datetime | None = None) -> bool:
        kind = data["kind"]
        if self.terminal_detail and kind != "telemetry":
            self._diagnostic("[RAW GATEWAY] %s", json.dumps(gateway_log_snapshot(data), separators=(",", ":")))
        if kind in METRIC_SUMMARY_KINDS:
            return await self._process_metric_summary(data)
        if kind == PDR_KIND:
            observed_at = received_at or datetime.now(timezone.utc)
            summary = parse_pdr_summary(data)
            gateway_id = getattr(self.config, "metrics_gateway_id", "")
            if not gateway_id:
                if "pdr" not in self._warned_metric_producers:
                    logger.warning("PDR ignorado (METRICS_GATEWAY_ID nao configurado)")
                    self._warned_metric_producers.add("pdr")
                return False
            connection = self._connection if (data["schema"] == PDR_COMMIT_SCHEMA and
                getattr(self.config, "pdr_serial_commit_enabled", False)) else None
            return await self._submit_work(partial(self._persist_pdr_summary, gateway_id, summary, connection,
                                                   received_at=observed_at),
                                           priority=_PDR_WORK_PRIORITY,
                                           description=f"[{collar_label(summary.collar_id)}] tipo=PDR")
        if kind == "telemetry":
            await self._save_and_broadcast(
                self._telemetry_packet(
                    data,
                    "[APP] " + json.dumps(data, separators=(",", ":")),
                    received_at,
                )
            )
            return True

        if kind == "command_response":
            request_id = data.get("requestId")
            future = self._pending_commands.get(request_id)
            matched = future is not None and not future.done()
            if matched:
                future.set_result(data)
                profile = data.get("activeProfileId")
                if (data.get("command") in {"GET_PROFILE", "GET_PROFILE_V2"} and data.get("ok") is True
                        and type(profile) is int and 0 <= profile <= 3
                        and data.get("profileControlVersion") in {1, 2}):
                    self.effective_profile = {
                        "profile_id": profile, "observed_at": datetime.now(timezone.utc).isoformat(),
                        "transition_state": data.get("state") if data.get("state") in {
                            "IDLE", "PREPARING", "COMMITTING", "VERIFYING", "COMPLETED", "ROLLING_BACK", "FAILED",
                        } else None,
                    }
            if not data.get("ok") or data.get("command") != "FENCE_POINT" or self.terminal_detail:
                log = logger.info if data.get("ok") else logger.warning
                response = "ACEITO" if data.get("ok") else "RECUSADO"
                origin = "GATEWAY RESPONDEU" if matched else "RESPOSTA DO GATEWAY RECEBIDA"
                command = gateway_log_field("command", data.get("command"))
                rows = [f"Comando: {base_value(command)} | Resultado: {response}",
                        f"Estado: {origin}"]
                detail = gateway_log_field("detail", data.get("detail"))
                if detail is not None:
                    rows.append(f"Detalhe: {detail}")
                if self.terminal_detail:
                    rows.append(f"Request ID: {base_value(gateway_log_field('requestId', request_id))}")
                log("%s", base_block("USB / GATEWAY | COMANDO", *rows, collar_id=data.get("collarId"),
                                     critical=not data.get("ok")))
            return True

        if kind == "fence_transfer":
            pending = self._fence_result
            awaiting_result = pending is not None and not pending.done()
            self._handle_fence_transfer(data)
            correlated = awaiting_result and pending.done()
            state = data.get("state")
            if state == "started":
                status = "GATEWAY REPORTOU INICIO DA TRANSFERENCIA"
            elif state == "confirmed" and correlated:
                status = "GATEWAY REPORTOU ACK DA COLEIRA"
            elif state == "confirmed":
                status = "RELATO DE CONFIRMACAO SEM CORRELACAO"
            elif state == "failed" and correlated:
                status = "FALHA REPORTADA PELO GATEWAY"
            elif state == "failed":
                status = "FAILED | Operacao atual: NAO ALTERADA"
            else:
                status = base_value(gateway_log_field("state", state))
            rows = [f"Estado: {status}"]
            detail = gateway_log_field("detail", data.get("detail"))
            if detail is not None:
                rows.append(f"Detalhe: {detail}")
            if state == "failed" or self.terminal_detail:
                request_id = gateway_log_field("requestId", data.get("requestId"))
                label = "Request ID recebido" if state == "failed" and not correlated else "Request ID"
                rows.append(f"{label}: {base_value(request_id)}")
            version = gateway_log_field("fenceVersion", data.get("fenceVersion"))
            title = f"CERCA | VERSAO {base_value(version)}"
            critical = state == "failed" and correlated
            if state == "failed" and not correlated:
                title = "CERCA | RELATO SEM CORRELACAO"
                if version is not None:
                    rows.append(f"Versao reportada: {version}")
            log = logger.warning if critical else logger.info
            log("%s", base_block(title, *rows, collar_id=data.get("collarId"), critical=critical))
            await self.hub.broadcast({"type": "fence_transfer", "data": data})
            return True

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

        if kind in {"collar_state", "collar_status", "offline"} and type(data.get("collarId")) is int:
            self._record_collar_contact(data, received_at or datetime.now(timezone.utc))
        return await self._process_gateway_event(data)

    def _record_collar_contact(self, data: dict, received_at: datetime) -> None:
        collar_id = data["collarId"]
        state = "offline" if data["kind"] == "offline" else str(data.get("state", "")).lower()
        if not 1 <= collar_id <= 254 or state not in {"online", "offline"}:
            return
        name = self._collar_name(collar_id)
        previous = self.collar_contacts.get(name, {})
        observed = received_at.isoformat()
        if observed < previous.get("observed_at", ""):
            return
        last_contact = previous.get("last_contact_at")
        age = data.get("lastSeenS")
        if type(age) is int and 0 <= age <= 0xFFFFFFFF:
            last_contact = (received_at - timedelta(seconds=age)).isoformat()
        elif state == "online":
            last_contact = observed
        self.collar_contacts[name] = {"state": state, "last_contact_at": last_contact, "observed_at": observed}

    async def _process_metric_summary(self, data: dict) -> bool:
        received_at = datetime.now(timezone.utc)
        setting = "metrics_installation_id" if data["kind"] == HDOP_SUMMARY_KIND else "metrics_gateway_id"
        producer_id = getattr(self.config, setting, "")
        summary = parse_metric_summary(data, producer_id)
        if not producer_id:
            if setting not in self._warned_metric_producers:
                logger.warning("metric summary ignored (%s not configured)", setting.upper())
                self._warned_metric_producers.add(setting)
            return False
        connection = self._connection if self._metric_acks is not None else None
        return await self._submit_work(partial(self._persist_metric_summary, summary, connection,
                                               received_at=received_at),
                                       priority=_METRIC_WORK_PRIORITY, description=metric_identity(summary))

    async def _persist_pdr_summary(self, gateway_id: str, summary: PdrSummary, connection=None, *,
                                   received_at: datetime | None = None) -> bool:
        result = await asyncio.to_thread(self.database.save_pdr_summary, gateway_id, summary)
        log = logger.info if result in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE) else logger.warning
        log("%s", base_pdr(summary, result.name, detail=self.terminal_detail))
        persisted = result in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE)
        if persisted and connection is not None:
            try:
                await self._send_serial_commit_command(
                    connection, "PDR_COMMITTED", collarId=summary.collar_id,
                    gatewaySessionId=summary.gateway_session_id, windowId=summary.window_id,
                )
            except OSError:
                logger.warning("[BASE] PDR | ACK apos COMMIT nao escrito | Gateway pode reenviar")
        if result is MetricSaveResult.INSERTED and received_at is not None:
            logger.info("%s", fpga_metric_diagnostic(summary, received_at))
        return persisted

    async def _persist_metric_summary(self, summary: MetricSummary, connection=None, *,
                                      received_at: datetime | None = None) -> bool:
        result = await asyncio.to_thread(self.database.save_metric_summary, summary)
        log = logger.info if result in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE) else logger.warning
        log("%s", base_metric(summary, result.name, detail=self.terminal_detail))
        if result not in (MetricSaveResult.INSERTED, MetricSaveResult.DUPLICATE):
            return False
        queue = self._metric_acks
        if (queue is not None and connection is not None and connection is self._connection
                and not self._stop.is_set()
                and (summary.kind == "RADIO" or summary.gateway_session_id is not None)):
            try:
                queue.put_nowait((connection, summary))
                if self.terminal_detail:
                    logger.info("%s", base_block(
                        f"ACK | {summary.kind}", "Estado: ACK ENFILEIRADO",
                        f"Sessao: {summary.session_id} | Janela: {summary.window_id}", collar_id=summary.collar_id,
                    ))
            except asyncio.QueueFull:
                self._refused_metric_acks += 1
                if self._refused_metric_acks == 1 or self._refused_metric_acks % 100 == 0:
                    logger.warning("%s", base_block(
                        f"AVISO | ACK {summary.kind}", "Estado: ACK NAO ENFILEIRADO",
                        f"Motivo: FILA CHEIA | Recusados: {self._refused_metric_acks}", collar_id=summary.collar_id,
                    ))
        if result is MetricSaveResult.INSERTED and received_at is not None:
            logger.info("%s", fpga_metric_diagnostic(summary, received_at))
        return True

    async def _write_metric_acks(self, queue: asyncio.Queue[tuple[object, MetricSummary]]) -> None:
        while True:
            connection, summary = await queue.get()
            try:
                await self._send_metric_ack(connection, summary)
            except Exception as exc:
                logger.warning("%s", base_block(
                    f"ERRO | ACK {summary.kind}", "Escrita Serial: FALHOU", f"Motivo: {base_value(exc)}",
                    collar_id=summary.collar_id, critical=True,
                ))

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
        rows = ["Escrita Serial: CONCLUIDA"]
        if self.terminal_detail:
            rows.append(f"Request ID: {message['requestId']} | Sessao: {summary.session_id} | Janela: {summary.window_id}")
        logger.info("%s", base_block(f"ACK | {summary.kind}", *rows, collar_id=summary.collar_id))

    @staticmethod
    def _write_metric_ack_line(connection, line: bytes) -> None:
        previous_timeout = connection.write_timeout
        try:
            connection.write_timeout = _METRIC_ACK_WRITE_SECONDS
            if connection.write(line) != len(line):
                raise OSError("incomplete metric confirmation")
        finally:
            connection.write_timeout = previous_timeout

    async def _process_gateway_event(self, data: dict) -> bool:
        timestamp = datetime.now(timezone.utc).isoformat()
        collar_id = self._collar_name(data["collarId"]) if isinstance(data.get("collarId"), int) else None
        kind = data.get("kind")
        message = {"type": "gateway_event", "data": data}
        if kind not in _GATEWAY_EVENT_KINDS:
            await self.hub.broadcast(message)
            return True
        if kind == "collar_state":
            event_kind = f"collar_{str(data.get('state', 'unknown')).lower()}"
        else:
            event_kind = str(data.get("subtype") or data.get("event") or kind or "gateway_event").lower()
        radio_event = kind == "event"
        name = event_kind.upper() if radio_event else str(data.get("state") or kind).upper()
        safe_name = gateway_log_field("subtype" if radio_event else "state", name) or "DESCONHECIDO"
        sync_enabled = self.config.central_sync_enabled
        try:
            inserted = await self._save_gateway_event(event_kind, timestamp, collar_id, data, sync_enabled)
        except GatewayEventConflictError:
            logger.error("%s", base_block(
                f"ERRO | SQLITE | {safe_name}", "Estado: IDENTIDADE COM CONTEUDO DIVERGENTE",
                collar_id=collar_id, critical=True,
            ))
            return False
        except sqlite3.Error as exc:
            logger.error("%s", base_block(
                f"ERRO | SQLITE | {safe_name}", "Estado: EVENTO RECEBIDO | SQLite: FALHA DE PERSISTENCIA",
                f"Motivo: {type(exc).__name__}", collar_id=collar_id, critical=True,
            ))
            return False
        if "deliveryId" in data and getattr(self.config, "event_serial_commit_enabled", False):
            try:
                await self._send_serial_commit_command(self._connection, "EVENT_COMMITTED",
                                                      deliveryId=data["deliveryId"])
            except OSError:
                logger.warning("[BASE] EVENTO | ACK apos COMMIT nao escrito | Gateway pode reenviar")
        if not inserted:
            logger.info("[BASE][%s] EVENTO | Duplicata ja persistida", collar_label(collar_id))
            return True
        if collar_id and event_kind in {"gps_lost", "gps_recovered"}:
            self.collar_gnss[collar_id] = {"type": event_kind, "at": timestamp}
        log = logger.warning if name in ALERT_EVENTS else logger.info
        log("%s", base_event(
            collar_id, safe_name, source="Gateway",
            sequence=gateway_log_field("sequence", data.get("sequence")),
            fence_version=gateway_log_field("fenceVersion", data.get("fenceVersion")),
            detail=self.terminal_detail,
            sync_enabled=sync_enabled,
        ))
        await self.hub.broadcast(message)
        return True

    async def _save_gateway_event(self, kind: str, timestamp: str, collar_id: str | None,
                                  data: dict, sync_enabled: bool) -> bool:
        for delay in (*_EVENT_SQLITE_RETRY_SECONDS, None):
            try:
                return await asyncio.to_thread(
                    self.database.save_gateway_event, kind, timestamp=timestamp, collar_id=collar_id,
                    payload=data, enqueue_for_central=sync_enabled,
                )
            except sqlite3.Error as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if delay is None or code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                await asyncio.sleep(delay)

    async def _send_serial_commit_command(self, connection, command: str, **fields) -> None:
        message = {"schema": GATEWAY_SERIAL_SCHEMA, "requestId": await self.next_request_id(),
                   "command": command, **fields}
        line = b"[CMD] " + json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n"
        async with self._write_lock:
            if connection is None or connection is not self._connection or not getattr(connection, "is_open", True):
                raise ConnectionError("gateway disconnected")
            if self._serial_needs_separator is connection:
                line = _SERIAL_RESYNC + line
            self._serial_needs_separator = connection
            write = asyncio.create_task(asyncio.to_thread(self._write_metric_ack_line, connection, line))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await write
                raise
            self._serial_needs_separator = None

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
            logger.info("[BASE] USB / GATEWAY | Comando simulado: %s", base_value(command))
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
            logger.info("[BASE] USB / GATEWAY | Comando simulado: %s | Request ID: %s",
                        base_value(command), request_id)
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
                logger.info("[BASE][%s] CERCA | Versao: %s | Confirmacao simulada",
                            collar_label(collar_id), base_value(fence["version"]))
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

    def collar_snapshot(self, data: dict) -> dict:
        name = data["collar_id"]
        return {**data, "contact": self.collar_contacts.get(name), "gnss_event": self.collar_gnss.get(name)}

    def as_dict(self):
        return {
            "status": self.state.status,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "serial_open": self._connection is not None and self.state.status == "online",
            "last_response_at": self.last_response_at if self.state.status == "online" and not self.config.mock_serial else None,
            "freshness_seconds": getattr(self.config, "collar_offline_seconds", 180),
            "serial_port": self.state.serial_port,
            "last_message": self.state.last_message,
            "last_seen": self.state.last_seen,
            "error": self.state.error,
            "error_kind": self.connection_error_kind,
            "mock": self.config.mock_serial,
            "commit_config": {
                "metrics": getattr(self.config, "metrics_serial_ack_enabled", False),
                "events": getattr(self.config, "event_serial_commit_enabled", False),
                "pdr": getattr(self.config, "pdr_serial_commit_enabled", False),
            },
        }
