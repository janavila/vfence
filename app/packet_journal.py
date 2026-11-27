"""Local, best-effort packet journal. Never owns the gateway serial port."""
from __future__ import annotations

import csv
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Same columns as vfence_pc_serial.py, for comparable experimental datasets.
_PACKET_CSV_FIELDS = (
    "hostTimeUtc", "gatewaySessionId", "gatewayMs", "direction", "profileId", "length", "valid",
    "version", "type", "typeId", "subtype", "subtypeId", "collarId", "sequence",
    "ackReq", "retry", "urgent", "rssiDbm", "snrDb", "txOk", "txBytesWritten",
    "collarSessionId", "positionOrdinal", "packetProfileId", "profileStep",
    "profileSource", "profileTarget", "profileOperationId", "profileWaitMs", "rawHex",
)


class PacketJournal:
    """Queue packet records without delaying the reader on disk writes."""

    def __init__(self, directory: str | Path, *, capacity: int = 2048):
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.jsonl_path = root / f"vfence_packets_{stamp}.jsonl"
        self.csv_path = root / f"vfence_packets_{stamp}.csv"
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=capacity)
        self._closing = threading.Event()
        self._failed = threading.Event()
        self.written = 0
        self.dropped = 0  # rejected on the reader side (full queue or failed writer)
        self.failed_writes = 0  # records already queued but not fully written
        self._jsonl = self.jsonl_path.open("x", encoding="utf-8", buffering=1)
        try:
            self._csv_file = self.csv_path.open("x", encoding="utf-8", newline="", buffering=1)
            try:
                self._csv = csv.DictWriter(self._csv_file, fieldnames=_PACKET_CSV_FIELDS, extrasaction="ignore")
                self._csv.writeheader()
                self._csv_file.flush()
            except BaseException:
                self._csv_file.close()
                raise
        except BaseException:
            self._jsonl.close()
            raise
        self._thread = threading.Thread(target=self._run, name="vfence-packet-journal", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._jsonl.close()
            self._csv_file.close()
            raise

    def submit(self, packet: dict) -> bool:
        """Timestamp at reception, enqueue immediately, never wait for disk."""
        if not self._closing.is_set() and not self._failed.is_set():
            record = {**packet, "hostTimeUtc": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
            try:
                self._queue.put_nowait(record)
                return True
            except queue.Full:
                pass
        self.dropped += 1
        if self.dropped == 1:
            logger.warning("[JOURNAL] Registro de pacote descartado; captura incompleta. Base continua operacional.")
        return False

    def _run(self):
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._failed.is_set():
                    self.failed_writes += 1
                    continue
                try:
                    self._jsonl.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    self._csv.writerow({field: record.get(field) for field in _PACKET_CSV_FIELDS})
                    self._jsonl.flush()
                    self._csv_file.flush()
                    self.written += 1
                except (OSError, ValueError, TypeError) as exc:
                    self.failed_writes += 1
                    self._failed.set()
                    logger.warning("[JOURNAL] Falha de gravacao (%s); captura incompleta. Base continua operacional.",
                                   type(exc).__name__)
        finally:
            for stream in (self._jsonl, self._csv_file):
                try:
                    stream.close()
                except OSError:
                    self._failed.set()
                    logger.warning("[JOURNAL] Falha ao fechar arquivo; confira a captura.")

    def close(self, timeout: float = 5.0):
        """Drain pending records on normal shutdown, bounded wait on I/O failure."""
        self._closing.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("[JOURNAL] Encerramento sem confirmacao de gravacao de todos os registros.")
        elif self.dropped or self.failed_writes:
            logger.warning("[JOURNAL] Captura incompleta | gravados=%d descartados=%d falhas=%d",
                           self.written, self.dropped, self.failed_writes)
