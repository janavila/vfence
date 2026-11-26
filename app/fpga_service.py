"""Coordinate real FPGA windows independently of the Gateway's Serial reader."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.fpga_window import MAX_GROUP_SIZE, MINUTE_MS, WINDOW_MINUTES, FpgaWindow
from app.jtag_atlantic import IsolatedJtagAtlantic, JtagOwnerAlive, parse_result, sample_command
from app.metric_protocol import MetricSummary
from app.pdr_protocol import PdrSummary
from app.profile_api import ProfileReport, apply_fpga_profile


logger = logging.getLogger("vfence.fpga")
SETTLED = {"IDLE", "COMPLETED", "FAILED"}
# Time allowed for a modulo-32-bit observation over Serial. An observation
# brackets the remote timestamp; it does not assume symmetric USB latency.
MAX_CLOCK_RTT = 0.250
# Fixed post-collection delivery interval. Nios II keeps BEGIN open until CLOSE.
DELIVERY_GRACE_SECONDS = 15
# Leave one packet deadline for a PDR-only sample before the final CLOSE.
JTAG_SAMPLE_BUDGET_SECONDS = 5
EVALUATION_SECONDS = WINDOW_MINUTES * MINUTE_MS // 1000


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _record(model, row):
    return model(**{field.name: row[field.name] for field in fields(model)})


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)  # SQLite CURRENT_TIMESTAMP is UTC.
    return stamp


@dataclass(frozen=True)
class GatewayClock:
    tick: int
    utc: datetime
    valid_until: datetime
    uncertainty: timedelta

    @classmethod
    def from_settings(cls, value):
        tick, error = value["gateway_ms"], value["uncertainty_ms"]
        utc, expires = _timestamp(value["utc"]), _timestamp(value["valid_until"])
        if (type(tick) is not int or not 0 <= tick <= 0xFFFFFFFF
                or type(error) is not int or not 0 <= error < 60_000
                or not utc < expires < utc + timedelta(milliseconds=0x80000000)):
            raise ValueError("referencia de tempo Gateway invalida")
        return cls(tick, utc, expires, timedelta(milliseconds=error))

    def source_bounds(self, row, start, end):
        delta = (row["started_at_ms"] - self.tick) & 0xFFFFFFFF
        if delta >= 0x80000000 or start < self.utc or end > self.valid_until:
            return None
        first = self.utc + timedelta(milliseconds=delta)
        last = first + timedelta(milliseconds=row["duration_ms"])
        if first - self.uncertainty >= start and last + self.uncertainty <= end:
            return first, last
        return None

    def contains(self, row, start, end):
        bounds = self.source_bounds(row, start, end)
        if bounds is None:
            return False
        try:
            persisted = _timestamp(row["persisted_at"])
        except (KeyError, TypeError, ValueError):
            return False
        return start <= persisted <= end and persisted + timedelta(seconds=1) >= bounds[1] - self.uncertainty


@dataclass(frozen=True)
class NetworkWindow:
    session: str
    first_id: int
    first_tick: int
    projected_start: float
    uncertainty: float

    @classmethod
    def from_reply(cls, reply, session, sent, received):
        if (reply.get("ok") is not True or reply.get("command") != "GET_METRIC_CLOCK_V1"
                or type(reply.get("clockVersion")) is not int or reply["clockVersion"] != 1
                or reply.get("gatewaySessionId") != session
                or reply.get("durationMs") != MINUTE_MS or not 0 <= received - sent <= MAX_CLOCK_RTT):
            return None
        tick = reply.get("tickMs")
        start = reply.get("windowStartMs")
        window_id = reply.get("windowId")
        if (any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in (tick, start))
                or not isinstance(window_id, str) or not re.fullmatch(r"[0-9a-f]{16}", window_id)):
            return None
        remaining = (start + MINUTE_MS - tick) & 0xFFFFFFFF
        identity = int(window_id, 16)
        if not 0 < remaining <= MINUTE_MS or not 0 < identity < 0xFFFFFFFFFFFFFFFF - WINDOW_MINUTES:
            return None
        # Include one remote tick of quantization, in addition to the full
        # uncertainty interval inferred from this particular round trip.
        return cls(session, identity + 1, (start + MINUTE_MS) & 0xFFFFFFFF,
                   (sent + received) / 2 + remaining / 1000, (received - sent) / 2 + 0.001)

    def includes(self, row):
        offset = int(row["window_id"], 16) - self.first_id
        return (0 <= offset < WINDOW_MINUTES and row["duration_ms"] == MINUTE_MS
                and row["started_at_ms"] == ((self.first_tick + MINUTE_MS * offset) & 0xFFFFFFFF))

    def compatible(self, later):
        if later is None or self.session != later.session:
            return False
        delta = later.first_id - self.first_id
        return (0 <= delta <= WINDOW_MINUTES + 1
                and ((later.first_tick - self.first_tick) & 0xFFFFFFFF) == delta * MINUTE_MS
                and abs(later.projected_start - self.projected_start - delta * MINUTE_MS / 1000)
                <= self.uncertainty + later.uncertainty)


class FpgaService:
    def __init__(self, config, database, reader, transport=IsolatedJtagAtlantic):
        self.config = config
        self.database = database
        self.reader = reader
        self.transport = transport
        self.stop = asyncio.Event()
        self.link = None
        self._last_network_clock = None
        self.history = deque(maxlen=24)
        self.status = {"connection": "disabled", "window": "idle", "last_tx": None, "last_rx": None,
                       "error": None, "result": None, "gateway": None}

    def snapshot(self):
        return {**self.status, "history": list(self.history)}

    async def command(self, command, expected, *, allow_result=False, record=True):
        sent = {"at": _utc_now(), "line": command, "direction": "TX"}
        try:
            line = await asyncio.to_thread(self.link.command, command)
        finally:
            if record and self.link.last_sent:
                self.status["last_tx"] = sent
                self.history.appendleft(sent)
                logger.info("[FPGA] TX %s", command)
        received = {"at": _utc_now(), "line": line, "direction": "RX"}
        if record:
            self.status["last_rx"] = received
            self.history.appendleft(received)
            logger.info("[FPGA] RX %s", line)
        if line not in expected and not (allow_result and line.startswith("VFENCE|P")):
            raise ValueError(f"resposta inesperada em {command.split()[0]}")
        return line

    async def reconcile(self):
        await self.command("PING", {"PONG"})
        line = await self.command("RESULT", {"ERR|NO_RESULT"}, allow_result=True)
        if line == "ERR|NO_RESULT":
            line = await self.command("CLOSE", {"ERR|NO_WINDOW", "ERR|NO_RESULT"}, allow_result=True)
        if line not in {"ERR|NO_RESULT", "ERR|NO_WINDOW"}:
            # The wire reply carries no window or producer identity. Archive
            # it, but do not use it to recommend or apply any profile.
            if not re.fullmatch(r"VFENCE\|P[0-3]\|[0-5]\|[01]", line):
                raise ValueError("resultado JTAG recuperado inválido")
            await self.commit_result({"at": _utc_now(), "window_id": None, "wire": line,
                                      "recovered": True}, recovering=True)
            logger.info("[FPGA] Resultado anterior arquivado na reconexao")

    async def gateway(self):
        if self.config.mock_serial or self.reader.state.status != "online":
            self.status["gateway"] = None
            return None
        try:
            reply = await self.reader.send_gateway_command("GET_PROFILE_V2", timeout_seconds=2)
            if reply.get("command") != "GET_PROFILE_V2" or reply.get("profileControlVersion") != 2 or reply.get("ok") is not True:
                raise ValueError("perfil fisico indisponivel")
            report = ProfileReport.model_validate(reply).model_dump()
        except (OSError, ValueError):
            self.status["gateway"] = None
            return None
        self.status["gateway"] = report
        return report

    def records(self, ids):
        return {collar: self.database.fpga_window_metrics(collar, 10, gateway_id=self.config.metrics_gateway_id)
                for collar in ids}

    async def network_window(self, session):
        sent = time.monotonic()
        try:
            reply = await self.reader.send_gateway_command("GET_METRIC_CLOCK_V1", timeout_seconds=MAX_CLOCK_RTT)
        except (OSError, TimeoutError):
            return None
        received = time.monotonic()
        clock = NetworkWindow.from_reply(reply, session, sent, received)
        if clock is not None:
            self._last_network_clock = (clock, received, self.reader.serial_generation)
        return clock

    def display_clock(self):
        if self._last_network_clock is None or self.reader.state.status != "online":
            return None
        clock, received, generation = self._last_network_clock
        if generation == self.reader.serial_generation and time.monotonic() - received <= 90:
            return clock
        return None

    def display_freshness(self, row, clock):
        if clock is None:
            return "unknown", None
        profile = (self.reader.effective_profile or {}).get("profile_id")
        if profile is None:
            return "unknown", None
        offset = int(row["window_id"], 16) - clock.first_id
        if (row["gateway_session_id"] != clock.session or profile != row["profile_id"]
                or row["started_at_ms"] != (clock.first_tick + offset * MINUTE_MS) & 0xFFFFFFFF):
            return "historical", None
        age = time.monotonic() - clock.projected_start - (offset + 1) * MINUTE_MS / 1000
        if age < -clock.uncertainty:
            return "unknown", None
        return ("recent" if age + clock.uncertainty <= 180 else "historical"), max(0, int(age * 1000))

    def aligned_hdop(self, hdop, pdr, clocks):
        offset = clocks.get(f"{pdr.gateway_session_id}/{pdr.collar_session_id}")
        if type(offset) is not int or hdop.gateway_received_at_ms is None:
            return None
        mapped = (hdop.started_at_ms + offset) & 0xFFFFFFFF
        after_end = (hdop.gateway_received_at_ms - mapped - hdop.duration_ms) & 0xFFFFFFFF
        return mapped if mapped == pdr.started_at_ms and after_end < 0x80000000 else None

    @staticmethod
    def _observed_window_ids(records, gateway_id, session):
        for windows in records.values():
            yield from (row["window_id"] for row in windows["pdr"]
                        if row["gateway_id"] == gateway_id and row["gateway_session_id"] == session)
            yield from (row["window_id"] for row in windows["radio"]
                        if row["producer_id"] == gateway_id and row["session_id"] == session)

    def samples(self, batch, reports, clocks, covers, *, network=None):
        for collar, windows in reports.items():
            for row in sorted(windows["pdr"], key=lambda value: value["window_id"]):
                if row["gateway_id"] != batch.gateway_id:
                    continue
                pdr = _record(PdrSummary, row)
                proven = row.get("tx_proven") == 1
                radios = (item for item in windows["radio"] if item["window_id"] == pdr.window_id)
                radio = next((item for item in radios if item["producer_id"] == batch.gateway_id
                              and item["session_id"] == batch.gateway_session and covers(item)
                              and (not proven or (item.get("timing_proven") == 1
                                   and item.get("collar_session_id") == pdr.collar_session_id
                                   and item.get("profile_id") == pdr.profile_id))), None)
                hdop = aligned = None
                for item in windows["hdop"]:
                    if item["producer_id"] != self.config.metrics_installation_id or item["session_id"] != pdr.collar_session_id:
                        continue
                    # Base assigns different producer IDs to HDOP and RADIO.
                    # Normalize only after checking the installation and boot.
                    summary = _record(MetricSummary, {**item, "producer_id": batch.gateway_id})
                    if proven:
                        if (item.get("timing_proven") == 1 and item["gateway_session_id"] == batch.gateway_session
                                and item.get("gateway_id") == batch.gateway_id and item.get("profile_id") == pdr.profile_id
                                and item["window_id"] == pdr.window_id and item["started_at_ms"] == pdr.started_at_ms
                                and covers(item)):
                            aligned = pdr.started_at_ms
                            hdop = summary
                            break
                        continue
                    aligned = self.aligned_hdop(summary, pdr, clocks)
                    if aligned is None:
                        continue
                    if network is not None:
                        arrived = (summary.gateway_received_at_ms - network.first_tick) & 0xFFFFFFFF
                        eligible = (network.includes({**item, "window_id": pdr.window_id, "started_at_ms": aligned})
                                    and arrived <= 1000 * (EVALUATION_SECONDS + DELIVERY_GRACE_SECONDS))
                    else:
                        eligible = (covers({**item, "started_at_ms": aligned})
                                    and covers({**item, "started_at_ms": summary.gateway_received_at_ms, "duration_ms": 0}))
                    if eligible:
                        hdop = summary
                        break
                batch.add(gateway_id=batch.gateway_id, pdr=pdr,
                          radio=_record(MetricSummary, radio) if radio else None,
                          hdop=hdop, aligned_hdop_start_ms=aligned)

    async def finish(self, line, profile, window_id):
        result = parse_result(line, profile)
        result.update(at=_utc_now(), window_id=window_id, ack=False, wire=line)
        self.status.update(result=result, window="result")
        logger.info("[FPGA] Resultado janela=%s %s", window_id, line)
        await self.commit_result(result)
        self.status.update(result={**result, "ack": True}, window="confirmed")

    async def commit_result(self, result, *, recovering=False):
        result_id = None
        if getattr(self.config, "windows_base", False):
            result_id = await asyncio.to_thread(self.database.save_windows_fpga_result, result, recovering=recovering)
        await self.command("ACK", {"OK|ACK"})
        if result_id is not None:
            await asyncio.to_thread(self.database.acknowledge_windows_fpga_result, result_id)

    async def pause(self, seconds=3):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            pass

    def same_gateway(self, current, original, generation):
        return (current is not None and current["state"] in SETTLED
                and self.reader.serial_generation == generation
                and all(current[field] == original[field] for field in (
                    "gatewaySessionId", "activeProfileId", "operationId",
                )))

    def session_changed(self, reports, sessions, gateway_session, includes):
        return any(
            row["gateway_id"] == self.config.metrics_gateway_id
            and row["gateway_session_id"] == gateway_session
            and (row.get("reason") == "session_changed"
                 or row["collar_session_id"] not in (None, "", session))
            and includes(row)
            for collar, session in sessions.items() for row in reports[collar]["pdr"]
        )

    async def accept_result(self, line, window_id, profile_report, generation, clock, start, observed, sessions,
                            *, network=None):
        clock_invalid = network is not None and not network.compatible(
            await self.network_window(profile_report["gatewaySessionId"])
        )
        if sessions:
            reports = await asyncio.to_thread(self.records, list(sessions))
            includes = network.includes if network else lambda row: clock.source_bounds(row, start, observed) is not None
            changed_session = self.session_changed(reports, sessions, profile_report["gatewaySessionId"],
                                                   includes)
        else:
            changed_session = False
        current = await self.gateway()
        now = _timestamp(_utc_now())
        if (self.stop.is_set() or not self.same_gateway(current, profile_report, generation)
                or changed_session or clock_invalid
                or (sessions and not network and not start <= observed <= now <= clock.valid_until)
                or (not sessions and parse_result(line, profile_report["activeProfileId"])["reason"] != 4)):
            self.status["window"] = "discarded"
            logger.warning("[FPGA] Resultado descartado | Sessao, perfil ou intervalo mudou")
            await self.command("ACK", {"OK|ACK"})
            return False
        await self.finish(line, profile_report["activeProfileId"], window_id)
        if network is not None and sessions and getattr(self.config, "fpga_auto_profile_enabled", False):
            await self.maybe_apply(profile_report, generation, network, sessions)
        return True

    async def maybe_apply(self, origin, generation, network, sessions):
        result = self.status["result"]
        roster = {int(name[3:]) for name in self.config.known_collar_ids}
        if result["reason"] not in (1, 2) or not result["ack"] or roster != sessions.keys():
            return
        expires = time.monotonic() + EVALUATION_SECONDS

        async def still_valid():
            if (self.stop.is_set() or time.monotonic() >= expires or
                    self.reader.serial_generation != generation or self.reader.state.status != "online"):
                return False
            if await self.network_window(network.session) is None:
                return False
            reports = await asyncio.to_thread(self.records, list(sessions))
            return not self.session_changed(reports, sessions, network.session,
                                            lambda row: int(row["window_id"], 16) >= network.first_id)

        try:
            applied = await apply_fpga_profile(self.config, self.reader, self.database, origin,
                                               result["profile"], still_valid, self.pause)
        except HTTPException as exc:
            logger.warning("[FPGA] Troca automatica interrompida | HTTP %s", exc.status_code)
            return
        if applied:
            self.status["gateway"] = applied
            logger.info("[FPGA] Perfil aplicado pelo Gateway: P%s", applied["activeProfileId"])
        else:
            logger.warning("[FPGA] Troca automatica sem confirmacao integral; aguarde nova avaliacao")

    async def evaluate(self, ids, clocks, profile_report, gateway_clocks=None, *, network=None,
                       require_proven=False):
        session = profile_report["gatewaySessionId"]
        profile = profile_report["activeProfileId"]
        serial_generation = self.reader.serial_generation
        initial = await asyncio.to_thread(self.records, ids)
        gateway_id = self.config.metrics_gateway_id
        watermark = max(self._observed_window_ids(initial, gateway_id, session), default="")
        window_id = str(uuid4())
        await self.command(f"BEGIN {profile}", {"OK|BEGIN"})
        start = _timestamp(_utc_now())
        started = time.monotonic()
        deadline = (network.projected_start + EVALUATION_SECONDS + DELIVERY_GRACE_SECONDS
                    if network else started + EVALUATION_SECONDS)
        clock = (gateway_clocks or {}).get(session)
        batch = None
        sent = set()
        used_sessions = {}
        observed = start
        self.status.update(window="collecting", window_id=window_id)
        while not self.stop.is_set():
            report = await self.gateway()
            if not self.same_gateway(report, profile_report, serial_generation):
                self.status["window"] = "discarded"
                await self.reconcile()
                return
            if time.monotonic() >= deadline:
                break
            reports = await asyncio.to_thread(self.records, ids)
            observed = min(_timestamp(_utc_now()), start + timedelta(
                seconds=min(EVALUATION_SECONDS, time.monotonic() - started)))
            includes = network.includes if network else lambda row: clock.source_bounds(row, start, observed) is not None
            if used_sessions and self.session_changed(reports, used_sessions, session, includes):
                self.status["window"] = "discarded"
                await self.reconcile()
                return

            def covers(row):
                if network is not None:
                    offset = int(row["window_id"], 16) - network.first_id
                    return network.includes(row) and (
                        time.monotonic() >= network.projected_start + (offset + 1) * MINUTE_MS / 1000 + network.uncertainty
                    )
                return clock is not None and clock.contains(row, start, observed)

            for windows in reports.values():
                windows["pdr"] = [row for row in windows["pdr"] if row["window_id"] > watermark
                                  and row["gateway_session_id"] == session and row["gateway_id"] == gateway_id
                                  and (not require_proven or (row.get("tx_proven") == 1
                                       and row.get("installation_id", self.config.metrics_installation_id)
                                       == self.config.metrics_installation_id))
                                  and covers(row)]
            available = [row for windows in reports.values() for row in windows["pdr"]
                         if row["profile_id"] == profile and row["collar_session_id"]]
            if batch is None and available:
                anchor = network.first_tick if network else min(available, key=lambda value: value["window_id"])["started_at_ms"]
                sessions = {}
                for row in sorted(available, key=lambda value: value["window_id"]):
                    sessions.setdefault(row["collar_id"], row["collar_session_id"])
                batch = FpgaWindow(gateway_id, session, anchor, profile, sessions, deadline)
            if batch is not None:
                for row in sorted(available, key=lambda value: value["window_id"]):
                    expected = batch.collar_sessions.setdefault(row["collar_id"], row["collar_session_id"])
                    if expected != row["collar_session_id"]:
                        self.status["window"] = "discarded"
                        await self.reconcile()
                        return
                self.samples(batch, reports, clocks, covers, network=network)
                for sample in batch.ready_samples():
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    key = sample.collar_id, sample.minute_index
                    identity = key if require_proven else (key, sample.valid_vote)
                    if identity in sent or (not sample.valid_vote and sample.pdr_x100 is None):
                        continue
                    if require_proven and network and not sample.valid_vote:
                        minute_end = network.projected_start + (sample.minute_index + 1) * MINUTE_MS / 1000
                        if now < min(minute_end + DELIVERY_GRACE_SECONDS, deadline - JTAG_SAMPLE_BUDGET_SECONDS):
                            continue
                    line = await self.command(sample_command(sample), {"OK|SAMPLE", "ERR|RESULT_PENDING"}, allow_result=True)
                    sent.add(identity)
                    used_sessions[sample.collar_id] = batch.collar_sessions[sample.collar_id]
                    if line == "ERR|RESULT_PENDING":
                        line = await self.command("RESULT", set(), allow_result=True)
                    if line != "OK|SAMPLE":
                        await self.accept_result(line, window_id, profile_report, serial_generation,
                                                 clock, start, observed, used_sessions, network=network)
                        return
            await self.command("PING", {"PONG"}, record=False)
            await self.pause(min(3, max(0, deadline - time.monotonic())))
        if self.stop.is_set():
            return
        line = await self.command("CLOSE", set(), allow_result=True)
        await self.accept_result(line, window_id, profile_report, serial_generation,
                                 clock, start, start + timedelta(seconds=EVALUATION_SECONDS), used_sessions, network=network)

    async def run(self):
        if not self.config.fpga_enabled or not getattr(self.config, "windows_base", False):
            return
        self.status["connection"] = "offline"
        roster = [re.fullmatch(r"COL([0-9]{1,3})", name) for name in self.config.known_collar_ids]
        ids = [int(match[1]) for match in roster if match]
        try:
            clocks = json.loads(self.config.hdop_clock_map)
            gateway_clocks = {key: GatewayClock.from_settings(value)
                              for key, value in json.loads(self.config.gateway_clock_map).items()}
            if not isinstance(clocks, dict) or len(ids) != len(roster) or not 1 <= len(ids) <= MAX_GROUP_SIZE or (
                len(set(ids)) != len(ids) or not all(1 <= collar <= 254 for collar in ids)
            ):
                raise ValueError
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            self.status["error"] = "Configuração FPGA inválida"
            logger.error("[FPGA] Configuracao invalida. Confira COLLAR_IDS e os mapas de tempo da FPGA")
            return
        if not clocks:
            logger.info("[FPGA] HDOP sincronizado usa identidade de janela do Gateway")
        if not gateway_clocks:
            logger.info("[FPGA] Referencia do Gateway sera consultada pela Serial; legados exigem mapa medido")
        logger.info("[FPGA] PDR por ordinal de posicao fica no historico; a votacao exige TX comprovado")
        retry_seconds = 3
        parked = None
        while not self.stop.is_set():
            if parked is not None:
                if not parked.released():
                    await self.pause(3)
                    continue
                parked = None
            try:
                self.link = await asyncio.to_thread(self.transport, self.config)
                await self.reconcile()
                self.status.update(connection="online", error=None)
                logger.info("[FPGA] CONECTADA | PONG CONFIRMADO")
                retry_seconds = 3
                while not self.stop.is_set():
                    report = await self.gateway()
                    ready = report and report["state"] in SETTLED and self.config.metrics_gateway_id
                    network = await self.network_window(report["gatewaySessionId"]) if ready else None
                    if network is not None:
                        await self.evaluate(ids, clocks, report, gateway_clocks, network=network, require_proven=True)
                    else:
                        self.status["window"] = "waiting_clock" if ready else "waiting"
                        await self.command("PING", {"PONG"}, record=False)
                        await self.pause()
            except JtagOwnerAlive as error:
                self.status.update(connection="offline", window="interrupted", error=str(error))
                logger.error("[FPGA] %s", error)
                parked = error
            except Exception as error:
                changed = self.status["error"] != str(error)
                self.status.update(connection="offline", window="interrupted", error=str(error))
                if changed:
                    logger.warning("[FPGA] %s | nova tentativa após intervalo", error)
            finally:
                if self.link is not None:
                    link, self.link = self.link, None
                    try:
                        await asyncio.to_thread(link.close)
                    except JtagOwnerAlive as error:
                        self.status.update(connection="offline", window="interrupted", error=str(error))
                        logger.error("[FPGA] %s", error)
                        parked = error
                    except OSError:
                        logger.warning("[FPGA] Falha ao fechar o transporte")
            await self.pause(retry_seconds)
            retry_seconds = min(30, retry_seconds * 2)
