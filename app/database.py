from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from app.metric_protocol import MetricSummary, validate_metric_summary
from app.pdr_protocol import PdrSummary
from app.protocol import ParsedPacket


logger = logging.getLogger(__name__)


class MetricSaveResult(Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    FAILED = "failed"


class GatewayEventConflictError(ValueError):
    pass


class Database:
    def __init__(self, database_url: str):
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError("apenas DATABASE_URL sqlite:/// é suportada")
        raw_path = database_url[len(prefix):]
        self.path = raw_path if raw_path == ":memory:" else str(Path(raw_path).resolve())
        self._lock = threading.RLock()
        self._memory = None
        if self.path == ":memory:":
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory.row_factory = sqlite3.Row

    @contextmanager
    def connect(self, *, timeout: float = 10):
        connection = self._memory or sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if self._memory is None:
                connection.close()

    def initialize(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collar_id TEXT NOT NULL UNIQUE,
                    last_seen TEXT NOT NULL, status TEXT NOT NULL,
                    latitude REAL, longitude REAL, zone TEXT,
                    satellites INTEGER, hdop REAL, rssi REAL, snr REAL,
                    last_sequence INTEGER
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collar_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    latitude REAL, longitude REAL, zone TEXT,
                    satellites INTEGER, hdop REAL, rssi REAL, snr REAL,
                    sequence INTEGER, raw_message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collar_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL, old_value TEXT, new_value TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_collar_timestamp
                    ON telemetry(collar_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON events(timestamp DESC);
                CREATE TABLE IF NOT EXISTS gateway_event_receipts (
                    delivery_id TEXT PRIMARY KEY, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS central_outbox (
                    edge_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS central_outbox_stream (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), generation TEXT NOT NULL
                );
                INSERT OR IGNORE INTO central_outbox_stream (singleton,generation)
                    VALUES (1,lower(hex(randomblob(16))));
                CREATE TABLE IF NOT EXISTS fence_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    desired_version INTEGER,
                    downloaded_version INTEGER,
                    pending_version INTEGER,
                    sent_version INTEGER,
                    confirmed_version INTEGER,
                    central_crc32 TEXT,
                    margin_attention_cm INTEGER,
                    margin_critical_cm INTEGER,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fence_points (
                    version INTEGER NOT NULL,
                    point_index INTEGER NOT NULL,
                    latitude_e6 INTEGER NOT NULL,
                    longitude_e6 INTEGER NOT NULL,
                    PRIMARY KEY (version, point_index)
                );
                CREATE TABLE IF NOT EXISTS fence_deliveries (
                    version INTEGER NOT NULL,
                    collar_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (version, collar_id)
                );
                CREATE TABLE IF NOT EXISTS metric_summaries (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('HDOP', 'RADIO')),
                    producer_id TEXT NOT NULL CHECK (length(trim(producer_id)) > 0),
                    collar_id INTEGER NOT NULL
                        CHECK (typeof(collar_id) = 'integer' AND collar_id BETWEEN 1 AND 254),
                    session_id TEXT NOT NULL
                        CHECK (length(session_id) = 16 AND session_id NOT GLOB '*[^0-9a-f]*'),
                    window_id TEXT NOT NULL
                        CHECK (length(window_id) = 16 AND window_id NOT GLOB '*[^0-9a-f]*'
                               AND window_id <> '0000000000000000'),
                    started_at_ms INTEGER NOT NULL
                        CHECK (typeof(started_at_ms) = 'integer' AND started_at_ms BETWEEN 0 AND 4294967295),
                    duration_ms INTEGER NOT NULL CHECK (duration_ms = 60000),
                    sample_count INTEGER NOT NULL CHECK (typeof(sample_count) = 'integer'),
                    hdop_mean_x100 INTEGER,
                    rssi_mean_x100 INTEGER,
                    snr_mean_x100 INTEGER,
                    hdop_mean_valid INTEGER,
                    rssi_mean_valid INTEGER,
                    snr_mean_valid INTEGER,
                    gateway_session_id TEXT,
                    gateway_received_at_ms INTEGER,
                    persisted_at TEXT NOT NULL,
                    UNIQUE (kind, producer_id, collar_id, session_id, window_id),
                    CHECK (
                        (gateway_session_id IS NULL AND gateway_received_at_ms IS NULL)
                        OR (kind = 'HDOP' AND gateway_session_id IS NOT NULL
                            AND length(gateway_session_id) = 16
                            AND gateway_session_id NOT GLOB '*[^0-9a-f]*'
                            AND typeof(gateway_received_at_ms) = 'integer'
                            AND gateway_received_at_ms BETWEEN 0 AND 4294967295)
                    ),
                    CHECK (
                        (kind = 'HDOP' AND rssi_mean_valid IS NULL AND snr_mean_valid IS NULL
                         AND rssi_mean_x100 IS NULL AND snr_mean_x100 IS NULL
                         AND (
                             (hdop_mean_valid IS 0 AND sample_count = 0 AND hdop_mean_x100 IS NULL)
                             OR (hdop_mean_valid IS 1 AND sample_count BETWEEN 1 AND 60
                                 AND typeof(hdop_mean_x100) = 'integer' AND hdop_mean_x100 BETWEEN 0 AND 65534)
                         ))
                        OR (kind = 'RADIO' AND hdop_mean_valid IS NULL AND hdop_mean_x100 IS NULL
                            AND (
                                (rssi_mean_valid IS 0 AND snr_mean_valid IS 0 AND sample_count = 0
                                 AND rssi_mean_x100 IS NULL AND snr_mean_x100 IS NULL)
                                OR (rssi_mean_valid IS 1 AND snr_mean_valid IS 1
                                    AND sample_count BETWEEN 1 AND 65535
                                    AND typeof(rssi_mean_x100) = 'integer'
                                    AND rssi_mean_x100 BETWEEN -32700 AND 32700
                                    AND typeof(snr_mean_x100) = 'integer'
                                    AND snr_mean_x100 BETWEEN -32768 AND 32767)
                            ))
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_metric_summaries_collar_kind_persisted
                    ON metric_summaries(collar_id, kind, persisted_at DESC);
                CREATE TABLE IF NOT EXISTS pdr_windows (
                    gateway_id TEXT NOT NULL,
                    gateway_session_id TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    collar_id INTEGER NOT NULL CHECK (collar_id BETWEEN 1 AND 254),
                    collar_session_id TEXT,
                    started_at_ms INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms = 60000),
                    profile_id INTEGER CHECK (profile_id BETWEEN 0 AND 3),
                    received_count INTEGER NOT NULL CHECK (received_count BETWEEN 0 AND 64),
                    estimated_tx_count INTEGER,
                    estimated_lost_count INTEGER,
                    pdr_x100 INTEGER,
                    valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
                    reason TEXT NOT NULL,
                    persisted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (gateway_id, gateway_session_id, window_id, collar_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pdr_windows_collar ON pdr_windows(collar_id, persisted_at DESC);
                CREATE TABLE IF NOT EXISTS proven_metric_windows (
                    gateway_id TEXT NOT NULL, installation_id TEXT NOT NULL,
                    gateway_session_id TEXT NOT NULL, window_id TEXT NOT NULL,
                    collar_id INTEGER NOT NULL CHECK (collar_id BETWEEN 1 AND 254),
                    evidence TEXT NOT NULL, persisted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (gateway_id, gateway_session_id, window_id, collar_id)
                );
                CREATE INDEX IF NOT EXISTS idx_proven_metric_collar ON proven_metric_windows(collar_id, window_id);
                CREATE TABLE IF NOT EXISTS windows_detected_collars (
                    collar_id INTEGER PRIMARY KEY CHECK (collar_id BETWEEN 1 AND 254),
                    first_detected_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS windows_fpga_results (
                    id INTEGER PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS profile_operations (
                    gateway_session TEXT NOT NULL, operation_id INTEGER NOT NULL,
                    profile_id INTEGER NOT NULL, participants TEXT NOT NULL, status TEXT NOT NULL,
                    report_json TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (gateway_session,operation_id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(fence_deliveries)")}
            if "confirmed_crc32" not in columns:
                db.execute("ALTER TABLE fence_deliveries ADD COLUMN confirmed_crc32 TEXT")
            db.execute("PRAGMA optimize")

    def detect_windows_collar(self, collar_id: int, timestamp: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO windows_detected_collars VALUES (?, ?)", (collar_id, timestamp))

    def windows_detected_collars(self) -> list[int]:
        with self._lock, self.connect() as db:
            return [row[0] for row in db.execute("SELECT collar_id FROM windows_detected_collars ORDER BY collar_id")]

    def save_windows_fpga_result(self, result: dict, *, recovering: bool = False) -> int:
        with self._lock, self.connect() as db:
            if recovering:
                pending = db.execute("""
                    SELECT id, result_json FROM windows_fpga_results
                    WHERE acknowledged = 0 ORDER BY id DESC LIMIT 1
                """).fetchone()
                if pending and json.loads(pending["result_json"])["wire"] == result["wire"]:
                    return pending["id"]
            return db.execute("""
                INSERT INTO windows_fpga_results(result_json, received_at) VALUES (?, ?)
            """, (json.dumps(result), result["at"])).lastrowid

    def acknowledge_windows_fpga_result(self, result_id: int) -> None:
        with self._lock, self.connect() as db:
            db.execute("UPDATE windows_fpga_results SET acknowledged = 1 WHERE id = ?", (result_id,))

    def latest_windows_fpga_result(self):
        with self._lock, self.connect() as db:
            row = db.execute("""
                SELECT result_json, acknowledged FROM windows_fpga_results ORDER BY id DESC LIMIT 1
            """).fetchone()
        return {**json.loads(row["result_json"]), "ack": bool(row["acknowledged"])} if row else None

    def save_pdr_summary(self, gateway_id: str, summary: PdrSummary) -> MetricSaveResult:
        identity = (gateway_id, summary.gateway_session_id, summary.window_id, summary.collar_id)
        content = (
            summary.collar_session_id, summary.started_at_ms, summary.duration_ms, summary.profile_id,
            summary.received_count, summary.estimated_tx_count, summary.estimated_lost_count,
            summary.pdr_x100, summary.valid, summary.reason,
        )
        try:
            with self._lock, self.connect() as db:
                cursor = db.execute("""
                    INSERT INTO pdr_windows (
                        gateway_id, gateway_session_id, window_id, collar_id,
                        collar_session_id, started_at_ms, duration_ms, profile_id, received_count,
                        estimated_tx_count, estimated_lost_count, pdr_x100, valid, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (gateway_id, gateway_session_id, window_id, collar_id) DO NOTHING
                    """, (*identity, *content))
                if cursor.rowcount:
                    return MetricSaveResult.INSERTED
                row = db.execute("""
                    SELECT collar_session_id, started_at_ms, duration_ms, profile_id, received_count,
                           estimated_tx_count, estimated_lost_count, pdr_x100, valid, reason
                    FROM pdr_windows
                    WHERE gateway_id = ? AND gateway_session_id = ? AND window_id = ? AND collar_id = ?
                    """, identity).fetchone()
                return MetricSaveResult.DUPLICATE if row and tuple(row) == content else MetricSaveResult.CONFLICT
        except sqlite3.Error:
            logger.debug("PDR summary write failed", exc_info=True)
            return MetricSaveResult.FAILED

    def save_metric_evidence(self, gateway_id, installation_id, summary):
        from dataclasses import asdict

        identity = (gateway_id, summary.gateway_session_id, summary.window_id, summary.collar_id)
        payload = json.dumps(asdict(summary), separators=(",", ":"), sort_keys=True)
        try:
            with self._lock, self.connect() as db:
                inserted = db.execute("""
                    INSERT INTO proven_metric_windows (
                        gateway_id, gateway_session_id, window_id, collar_id, installation_id, evidence
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (gateway_id, gateway_session_id, window_id, collar_id) DO NOTHING
                    """, (*identity, installation_id, payload))
                if inserted.rowcount:
                    return MetricSaveResult.INSERTED
                previous = db.execute("""
                    SELECT installation_id,evidence FROM proven_metric_windows
                    WHERE gateway_id = ? AND gateway_session_id = ? AND window_id = ? AND collar_id = ?
                    """, identity).fetchone()
                return (MetricSaveResult.DUPLICATE if previous and tuple(previous) == (installation_id, payload)
                        else MetricSaveResult.CONFLICT)
        except sqlite3.Error:
            logger.debug("metric evidence write failed", exc_info=True)
            return MetricSaveResult.FAILED

    def proven_windows(self, collar_id, limit, *, gateway_id=None, gateway_session=None):
        from app.metric_evidence import MetricEvidence

        with self._lock, self.connect() as db:
            rows = db.execute("""
                WITH sessions AS (
                    SELECT gateway_id, gateway_session_id, MIN(rowid) AS first_seen
                    FROM proven_metric_windows GROUP BY gateway_id, gateway_session_id
                )
                SELECT e.* FROM proven_metric_windows AS e JOIN sessions USING (gateway_id, gateway_session_id)
                WHERE collar_id = :collar AND (:gateway IS NULL OR gateway_id = :gateway)
                    AND (:session IS NULL OR gateway_session_id = :session)
                ORDER BY first_seen DESC, window_id DESC LIMIT :limit
                """, {"collar": collar_id, "limit": limit, "gateway": gateway_id,
                      "session": gateway_session}).fetchall()
        return [MetricEvidence(**json.loads(row["evidence"])).project(
            row["gateway_id"], row["installation_id"], row["persisted_at"]
        ) for row in rows]

    def fpga_window_metrics(self, collar_id, limit, *, gateway_id=None):
        operational = self.link_metrics(collar_id, limit)["windows"]
        records = self.proven_windows(collar_id, limit, gateway_id=gateway_id)
        # Keep legacy invalidations such as session_changed for the decision
        # guard, without letting estimates override measured windows.
        return {kind: [record[kind] for record in records] + operational[kind] for kind in operational}

    def public_link_metrics(self, collar_id, limit, *, gateway_id=None, gateway_session=None):
        snapshot = self.link_metrics(collar_id, limit)
        proof = self.proven_windows(collar_id, limit, gateway_id=gateway_id, gateway_session=gateway_session)
        if not proof and gateway_session:
            proof = self.proven_windows(collar_id, limit, gateway_id=gateway_id)
        if proof:
            snapshot["legacy_windows"] = snapshot["windows"]
            snapshot["windows"] = {kind: [bundle[kind] for bundle in proof] for kind in snapshot["windows"]}
        return snapshot

    def list_pdr(self, collar_id: int, limit: int) -> list[dict]:
        with self._lock, self.connect() as db:
            # The session IDs are random. A late row from a known old session
            # must not displace the newest observed session or window.
            rows = db.execute("""
                WITH sessions AS (
                    SELECT gateway_id, gateway_session_id, MIN(rowid) AS first_seen
                    FROM pdr_windows
                    GROUP BY gateway_id, gateway_session_id
                )
                SELECT report.* FROM pdr_windows AS report
                JOIN sessions USING (gateway_id, gateway_session_id)
                WHERE report.collar_id = ?
                ORDER BY sessions.first_seen DESC, report.window_id DESC LIMIT ?
                """, (collar_id, limit)).fetchall()
        return [{**dict(row), "valid": bool(row["valid"])} for row in rows]

    def metric_collars(self) -> list[dict]:
        """List the most recent commit per metric kind for each observed collar."""
        with self._lock, self.connect() as db:
            rows = db.execute("""
                SELECT collar_id, LOWER(kind) AS kind, MAX(persisted_at) AS latest
                FROM metric_summaries GROUP BY collar_id, kind
                UNION ALL
                SELECT collar_id, 'pdr', MAX(persisted_at)
                FROM pdr_windows GROUP BY collar_id
                UNION ALL
                SELECT proven.collar_id, kinds.kind, MAX(proven.persisted_at)
                FROM proven_metric_windows AS proven
                CROSS JOIN (SELECT 'pdr' AS kind UNION ALL SELECT 'radio' UNION ALL SELECT 'hdop') AS kinds
                GROUP BY proven.collar_id, kinds.kind
                ORDER BY collar_id
            """).fetchall()
        collars = {}
        for row in rows:
            latest = collars.setdefault(row["collar_id"], {})
            stamp = datetime.fromisoformat(row["latest"])
            stamp = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            if row["kind"] not in latest or stamp > datetime.fromisoformat(latest[row["kind"]]):
                latest[row["kind"]] = stamp.isoformat()
        return [{"collar_id": collar, "latest_persisted": latest} for collar, latest in collars.items()]

    def link_metrics(self, collar_id: int, limit: int) -> dict:
        with self._lock, self.connect() as db:
            position = db.execute(
                "SELECT timestamp,zone,hdop,rssi,snr FROM telemetry WHERE collar_id=? "
                "ORDER BY timestamp DESC,id DESC LIMIT 1", (f"COL{collar_id:02d}",),
            ).fetchone()
            windows = {}
            for kind in ("HDOP", "RADIO"):
                # Session IDs are random. Order sessions by their first insertion
                # so a late window cannot make a previously observed session current.
                # Fixed-width hexadecimal window IDs sort by sequence within a session.
                windows[kind.lower()] = [dict(row) for row in db.execute("""
                    WITH sessions AS (
                        SELECT producer_id, session_id, ROW_NUMBER() OVER (
                            PARTITION BY producer_id ORDER BY MIN(id) DESC
                        ) AS session_rank
                        FROM metric_summaries
                        WHERE collar_id = :collar_id AND kind = :kind
                        GROUP BY producer_id, session_id
                    ),
                    ranked_windows AS (
                        SELECT summary.id, ROW_NUMBER() OVER (
                            PARTITION BY summary.producer_id ORDER BY summary.window_id DESC
                        ) AS window_rank
                        FROM metric_summaries AS summary
                        JOIN sessions USING (producer_id, session_id)
                        WHERE summary.collar_id = :collar_id AND summary.kind = :kind
                            AND sessions.session_rank = 1
                    )
                    SELECT summary.* FROM ranked_windows
                    JOIN metric_summaries AS summary USING (id)
                    WHERE window_rank <= :limit
                    ORDER BY window_rank, summary.persisted_at DESC, summary.id DESC
                """, {"collar_id": collar_id, "kind": kind, "limit": limit})]
            windows["pdr"] = self.list_pdr(collar_id, limit)
        return {"instant": dict(position) if position else None, "windows": windows}

    def start_profile_operation(self, session: str, reported_id: int, target: int, collars: list[int]) -> int:
        with self._lock, self.connect() as db:
            local = db.execute("SELECT COALESCE(MAX(operation_id),0) FROM profile_operations WHERE gateway_session=?",
                               (session,)).fetchone()[0]
            operation = max(local, reported_id) + 1
            if operation > 0xFFFFFFFF:
                raise ValueError("profile operation identity exhausted")
            db.execute(
                "INSERT INTO profile_operations VALUES (?,?,?,?, 'REQUESTED',NULL,?)",
                (session, operation, target, json.dumps(collars), datetime.now(timezone.utc).isoformat()),
            )
        return operation

    def record_profile_operation(self, session: str, operation: int, report: dict) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE profile_operations SET status=?,report_json=?,updated_at=? "
                "WHERE gateway_session=? AND operation_id=?",
                (report["state"], json.dumps(report), datetime.now(timezone.utc).isoformat(), session, operation),
            )

    def profile_history(self, limit: int) -> list[dict]:
        with self._lock, self.connect() as db:
            rows = db.execute(
                "SELECT * FROM profile_operations ORDER BY updated_at DESC,operation_id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{**dict(row), "participants": json.loads(row["participants"]),
                 "report_json": json.loads(row["report_json"]) if row["report_json"] else None} for row in rows]

    def save_metric_summary(self, summary: MetricSummary) -> MetricSaveResult:
        try:
            validate_metric_summary(summary)
            identity = (summary.kind, summary.producer_id, summary.collar_id, summary.session_id, summary.window_id)
            content = (
                summary.started_at_ms, summary.duration_ms, summary.sample_count,
                summary.hdop_mean_x100, summary.rssi_mean_x100, summary.snr_mean_x100,
                summary.hdop_mean_valid, summary.rssi_mean_valid, summary.snr_mean_valid,
            )
            with self._lock, self.connect() as db:
                cursor = db.execute("""
                    INSERT INTO metric_summaries (
                        kind, producer_id, collar_id, session_id, window_id, started_at_ms, duration_ms,
                        sample_count, hdop_mean_x100, rssi_mean_x100, snr_mean_x100, hdop_mean_valid,
                        rssi_mean_valid, snr_mean_valid, gateway_session_id, gateway_received_at_ms, persisted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (kind, producer_id, collar_id, session_id, window_id) DO NOTHING
                    """, (*identity, *content, summary.gateway_session_id, summary.gateway_received_at_ms,
                          datetime.now(timezone.utc).isoformat()))
                if cursor.rowcount == 1:
                    result = MetricSaveResult.INSERTED
                else:
                    # Recepcao e persistencia descrevem a primeira observacao da janela.
                    row = db.execute("""
                        SELECT started_at_ms, duration_ms, sample_count, hdop_mean_x100, rssi_mean_x100,
                               snr_mean_x100, hdop_mean_valid, rssi_mean_valid, snr_mean_valid
                        FROM metric_summaries
                        WHERE kind = ? AND producer_id = ? AND collar_id = ? AND session_id = ? AND window_id = ?
                        """, identity).fetchone()
                    if row is None:
                        result = MetricSaveResult.FAILED
                    else:
                        result = MetricSaveResult.DUPLICATE if tuple(row) == content else MetricSaveResult.CONFLICT
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            logger.debug("metric summary write failed", exc_info=True)
            return MetricSaveResult.FAILED
        return result

    @staticmethod
    def _queue(conn, item_type: str, data: dict, timestamp: str) -> int:
        cursor = conn.execute(
            "INSERT INTO central_outbox (item_type,data_json,created_at) VALUES (?,?,?)",
            (item_type, json.dumps(data, separators=(",", ":"), ensure_ascii=False), timestamp),
        )
        return cursor.lastrowid

    def save_packet(self, packet: ParsedPacket, enqueue_for_central: bool = False):
        stamp = packet.timestamp.isoformat()
        with self._lock, self.connect() as db:
            previous = db.execute("SELECT zone FROM collars WHERE collar_id = ?", (packet.collar_id,)).fetchone()
            old_zone = previous["zone"] if previous else None
            db.execute("""INSERT INTO telemetry
                (collar_id,timestamp,latitude,longitude,zone,satellites,hdop,rssi,snr,sequence,raw_message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                packet.collar_id, stamp, packet.latitude, packet.longitude, packet.zone,
                packet.satellites, packet.hdop, packet.rssi, packet.snr,
                packet.sequence, packet.raw_message,
            ))
            db.execute("""INSERT INTO collars
                (collar_id,last_seen,status,latitude,longitude,zone,satellites,hdop,rssi,snr,last_sequence)
                VALUES (?,?,'online',?,?,?,?,?,?,?,?)
                ON CONFLICT(collar_id) DO UPDATE SET
                last_seen=excluded.last_seen,status='online',
                latitude=COALESCE(excluded.latitude,collars.latitude),
                longitude=COALESCE(excluded.longitude,collars.longitude),
                zone=COALESCE(excluded.zone,collars.zone),
                satellites=COALESCE(excluded.satellites,collars.satellites),
                hdop=COALESCE(excluded.hdop,collars.hdop),
                rssi=COALESCE(excluded.rssi,collars.rssi),
                snr=COALESCE(excluded.snr,collars.snr),last_sequence=excluded.last_sequence""", (
                packet.collar_id, stamp, packet.latitude, packet.longitude, packet.zone,
                packet.satellites, packet.hdop, packet.rssi, packet.snr, packet.sequence,
            ))
            event = None
            if packet.zone is not None and old_zone != packet.zone:
                cursor = db.execute("""INSERT INTO events
                    (collar_id,timestamp,event_type,old_value,new_value)
                    VALUES (?,?,'zone_change',?,?)""", (packet.collar_id, stamp, old_zone, packet.zone))
                event = {"id": cursor.lastrowid, "collar_id": packet.collar_id,
                         "timestamp": stamp, "event_type": "zone_change",
                         "old_value": old_zone, "new_value": packet.zone}
            if enqueue_for_central and packet.type == "POS":
                self._queue(db, "telemetry", {
                    "collar_id": packet.collar_id,
                    "ts": stamp,
                    "lat": packet.latitude,
                    "lon": packet.longitude,
                    "zone": packet.zone,
                    "satellites": packet.satellites,
                    "hdop": packet.hdop,
                    "battery_pct": packet.battery_pct,
                    "fence_version": packet.fence_version,
                    "rssi": packet.rssi,
                    "snr": packet.snr,
                }, stamp)
                if event:
                    self._queue(db, "event", {
                        "collar_id": packet.collar_id,
                        "kind": "zone_change",
                        "zone": packet.zone,
                        "ts": stamp,
                        "detail": f"{old_zone or 'none'}->{packet.zone}",
                    }, stamp)
            collar = self._collar_snapshots(db, packet.collar_id)[0]
            return collar, event

    @staticmethod
    def _collar_snapshots(db, collar_id=None):
        # The legacy summary retains non-null values. Keep the latest sample
        # and last valid fix separate so consumers can report their provenance.
        where = "" if collar_id is None else "WHERE c.collar_id = ?"
        rows = db.execute(f"""
            SELECT c.*, t.timestamp AS position_at, t.latitude AS position_lat,
                t.longitude AS position_lon, t.zone AS position_zone,
                t.satellites AS position_satellites, t.hdop AS position_hdop,
                t.rssi AS position_rssi, t.snr AS position_snr, t.sequence AS position_sequence,
                t.raw_message AS position_raw, f.timestamp AS fix_at,
                f.latitude AS fix_lat, f.longitude AS fix_lon
            FROM collars c
            LEFT JOIN telemetry t ON t.id = (SELECT id FROM telemetry WHERE collar_id = c.collar_id
                ORDER BY timestamp DESC, id DESC LIMIT 1)
            LEFT JOIN telemetry f ON f.id = (SELECT id FROM telemetry WHERE collar_id = c.collar_id
                AND zone IS NOT NULL AND zone != 'GNSS_INVALIDO'
                AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
                ORDER BY timestamp DESC, id DESC LIMIT 1)
            {where} ORDER BY c.collar_id
            """, () if collar_id is None else (collar_id,))
        snapshots = []
        for row in rows:
            data = dict(row)
            raw = data.pop("position_raw")
            fields = ("at", "lat", "lon", "zone", "satellites", "hdop", "rssi", "snr", "sequence")
            position = {key: data.pop(f"position_{key}") for key in fields}
            if raw and raw.startswith("[APP] "):
                try:
                    report = json.loads(raw[6:])
                except ValueError:
                    report = {}
                if isinstance(report, dict):
                    for source, target, maximum in (("battery", "battery_pct", 100),
                                                     ("fenceVersion", "fence_version", 65535),
                                                     ("distanceBorderM", "distance_border_m", 65534)):
                        value = report.get(source)
                        if type(value) is int and 0 <= value <= maximum:
                            position[target] = value
            fix = {key: data.pop(f"fix_{key}") for key in ("at", "lat", "lon")}
            snapshots.append({**data, "position": position if position["at"] else None,
                              "last_fix": fix if fix["at"] else None})
        return snapshots

    def list_collars(self):
        with self._lock, self.connect() as db:
            return self._collar_snapshots(db)

    def get_collar(self, collar_id: str):
        with self._lock, self.connect() as db:
            return next(iter(self._collar_snapshots(db, collar_id)), None)

    def telemetry(self, collar_id: str, limit: int = 100):
        with self._lock, self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM telemetry WHERE collar_id = ? ORDER BY timestamp DESC LIMIT ?",
                (collar_id, limit)).fetchall()]

    def events(self, limit: int = 100):
        with self._lock, self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)).fetchall()]

    def save_gateway_event(self, kind: str, *, timestamp: str, collar_id: str | None,
                           payload: dict, enqueue_for_central: bool) -> bool:
        # Para eventos do Gateway, new_value preserva o JSON recebido. As
        # transicoes de zona continuam usando old_value e new_value como antes.
        detail = json.dumps(payload, separators=(",", ":"))
        local_collar = collar_id or "GATEWAY"
        with self._lock, self.connect(timeout=0.25) as db:
            delivery_id = payload.get("deliveryId")
            if delivery_id is not None:
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                cursor = db.execute(
                    "INSERT INTO gateway_event_receipts (delivery_id,digest) VALUES (?,?) "
                    "ON CONFLICT(delivery_id) DO NOTHING",
                    (delivery_id, digest),
                )
                if cursor.rowcount == 0:
                    existing = db.execute(
                        "SELECT digest FROM gateway_event_receipts WHERE delivery_id=?", (delivery_id,),
                    ).fetchone()
                    if existing["digest"] != digest:
                        raise GatewayEventConflictError("gateway event delivery identity conflict")
                    return False
            db.execute(
                "INSERT INTO events (collar_id,timestamp,event_type,new_value) VALUES (?,?,?,?)",
                (local_collar, timestamp, kind, detail),
            )
            if enqueue_for_central:
                self._queue(db, "event", {
                    "collar_id": collar_id, "kind": kind, "zone": None,
                    "detail": detail, "ts": timestamp,
                }, timestamp)
        return True

    def enqueue_event(self, kind: str, *, timestamp: str, collar_id: str | None = None,
                      zone: str | None = None, detail: str | None = None) -> int:
        with self._lock, self.connect() as db:
            return self._queue(db, "event", {
                "collar_id": collar_id, "kind": kind, "zone": zone,
                "detail": detail, "ts": timestamp,
            }, timestamp)

    def enqueue_delivery(self, collar_id: str, version: int, status: str,
                         timestamp: str, *, crc32: str | None = None,
                         detail: str | None = None) -> int:
        data = {"collar_id": collar_id, "fence_version": version, "status": status}
        if crc32 is not None:
            data["crc32"] = crc32
        if detail is not None:
            data["detail"] = detail
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO fence_deliveries(version,collar_id,status,detail,updated_at,confirmed_crc32)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(version,collar_id) DO UPDATE SET
                   status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at,
                   confirmed_crc32=excluded.confirmed_crc32""",
                (version, collar_id, status, detail, timestamp, crc32 if status == "confirmed" else None),
            )
            return self._queue(db, "delivery", data, timestamp)

    def confirmed_recipients(self, version: int, crc: str) -> set[str]:
        with self._lock, self.connect() as db:
            return {row[0] for row in db.execute(
                "SELECT collar_id FROM fence_deliveries WHERE version=? AND status='confirmed' AND confirmed_crc32=?",
                (version, crc),
            )}

    def outbox_batch(self, limit: int = 100) -> list[dict]:
        with self._lock, self.connect() as db:
            return self._outbox_batch(db, limit)

    def outbox_snapshot(self, limit: int) -> tuple[str, list[dict]]:
        with self._lock, self.connect() as db:
            return self._outbox_generation(db), self._outbox_batch(db, limit)

    @staticmethod
    def _outbox_batch(db, limit: int) -> list[dict]:
        rows = db.execute(
            "SELECT edge_seq,item_type,data_json FROM central_outbox ORDER BY edge_seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"edge_seq": row["edge_seq"], "type": row["item_type"],
                 "data": json.loads(row["data_json"])} for row in rows]

    def acknowledge_outbox(self, edge_seq: int) -> int:
        with self._lock, self.connect() as db:
            cursor = db.execute("DELETE FROM central_outbox WHERE edge_seq <= ?", (edge_seq,))
            return cursor.rowcount

    def outbox_generation(self) -> str:
        with self._lock, self.connect() as db:
            return self._outbox_generation(db)

    @staticmethod
    def _outbox_generation(db) -> str:
        return db.execute("SELECT generation FROM central_outbox_stream WHERE singleton=1").fetchone()[0]

    def acknowledge_confirmed_batch(self, generation: str, sequences: list[int]) -> int:
        with self._lock, self.connect() as db:
            if self._outbox_generation(db) != generation:
                raise ValueError("outbox generation changed before acknowledgement")
            result = db.executemany("DELETE FROM central_outbox WHERE edge_seq=?",
                                    ((sequence,) for sequence in sequences))
            return result.rowcount

    def outbox_size(self) -> int:
        with self._lock, self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM central_outbox").fetchone()[0])

    def outbox_health(self) -> dict:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS size, MIN(created_at) AS oldest FROM central_outbox").fetchone()
            return {"outbox_size": row["size"], "outbox_oldest_at": row["oldest"],
                    "outbox_pressure": row["size"] >= 10000}

    def save_pending_fence(self, fence: dict, timestamp: str) -> None:
        version = int(fence["version"])
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM fence_points WHERE version = ?", (version,))
            db.executemany(
                """INSERT INTO fence_points(version,point_index,latitude_e6,longitude_e6)
                   VALUES(?,?,?,?)""",
                [(version, index, int(point[0]), int(point[1]))
                 for index, point in enumerate(fence["points_e6"])],
            )
            db.execute(
                """INSERT INTO fence_state(singleton_id,desired_version,downloaded_version,
                       pending_version,central_crc32,margin_attention_cm,margin_critical_cm,
                       last_error,updated_at)
                   VALUES(1,?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                   desired_version=excluded.desired_version,
                   downloaded_version=excluded.downloaded_version,
                   pending_version=excluded.pending_version,
                   central_crc32=excluded.central_crc32,
                   margin_attention_cm=excluded.margin_attention_cm,
                   margin_critical_cm=excluded.margin_critical_cm,
                   last_error=NULL,updated_at=excluded.updated_at""",
                (version, version, version, fence["crc32"].upper(),
                 int(fence["margin_attention_cm"]), int(fence["margin_critical_cm"]),
                 timestamp),
            )

    def set_desired_fence(self, version: int | None, timestamp: str) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO fence_state(singleton_id,desired_version,updated_at)
                   VALUES(1,?,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                   desired_version=excluded.desired_version,updated_at=excluded.updated_at""",
                (version, timestamp),
            )

    def set_fence_progress(self, *, sent_version: int | None = None,
                           confirmed_version: int | None = None,
                           error: str | None = None, timestamp: str) -> None:
        with self._lock, self.connect() as db:
            current = db.execute("SELECT * FROM fence_state WHERE singleton_id=1").fetchone()
            desired = current["desired_version"] if current else None
            downloaded = current["downloaded_version"] if current else None
            pending = current["pending_version"] if current else None
            crc = current["central_crc32"] if current else None
            margin_attention = current["margin_attention_cm"] if current else None
            margin_critical = current["margin_critical_cm"] if current else None
            old_sent = current["sent_version"] if current else None
            old_confirmed = current["confirmed_version"] if current else None
            if confirmed_version is not None:
                pending = None
            db.execute(
                """INSERT OR REPLACE INTO fence_state
                   (singleton_id,desired_version,downloaded_version,pending_version,
                    sent_version,confirmed_version,central_crc32,margin_attention_cm,
                    margin_critical_cm,last_error,updated_at)
                   VALUES(1,?,?,?,?,?,?,?,?,?,?)""",
                (desired, downloaded, pending,
                 sent_version if sent_version is not None else old_sent,
                 confirmed_version if confirmed_version is not None else old_confirmed,
                 crc, margin_attention, margin_critical, error, timestamp),
            )

    def fence_state(self) -> dict:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM fence_state WHERE singleton_id=1").fetchone()
            return dict(row) if row else {
                "desired_version": None, "downloaded_version": None,
                "pending_version": None, "sent_version": None,
                "confirmed_version": None, "central_crc32": None,
                "last_error": None,
            }

    def fence_snapshot(self) -> dict:
        """Return the local fence state plus map-ready persisted vertices."""
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM fence_state WHERE singleton_id=1").fetchone()
            if row is None:
                return {
                    "desired_version": None,
                    "downloaded_version": None,
                    "pending_version": None,
                    "sent_version": None,
                    "confirmed_version": None,
                    "central_crc32": None,
                    "margin_attention_cm": None,
                    "margin_critical_cm": None,
                    "last_error": None,
                    "updated_at": None,
                    "status": "not_available",
                    "point_count": 0,
                    "points": [],
                }

            data = dict(row)
            version = data["downloaded_version"]
            points = []
            if version is not None:
                rows = db.execute(
                    """SELECT point_index,latitude_e6,longitude_e6 FROM fence_points
                       WHERE version=? ORDER BY point_index""",
                    (version,),
                ).fetchall()
                points = [
                    {
                        "index": point["point_index"],
                        "latitude_e6": point["latitude_e6"],
                        "longitude_e6": point["longitude_e6"],
                        "latitude": point["latitude_e6"] / 1_000_000,
                        "longitude": point["longitude_e6"] / 1_000_000,
                    }
                    for point in rows
                ]

            if version is None:
                status = "not_available"
            elif data["pending_version"] is not None:
                status = "pending"
            elif data["confirmed_version"] == version:
                status = "synchronized"
            else:
                status = "downloaded"
            data.update(status=status, point_count=len(points), points=points)
            return data

    def fence_by_version(self, version: int) -> dict | None:
        with self._lock, self.connect() as db:
            state = db.execute(
                "SELECT * FROM fence_state WHERE singleton_id=1 AND downloaded_version=?",
                (version,),
            ).fetchone()
            if state is None:
                return None
            points = db.execute(
                """SELECT latitude_e6,longitude_e6 FROM fence_points
                   WHERE version=? ORDER BY point_index""", (version,)
            ).fetchall()
            return {
                "version": version,
                "crc32": state["central_crc32"],
                "points_e6": [[row["latitude_e6"], row["longitude_e6"]] for row in points],
            }
