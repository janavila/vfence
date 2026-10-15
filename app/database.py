from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.protocol import ParsedPacket


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
    def connect(self):
        connection = self._memory or sqlite3.connect(self.path, timeout=10)
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
                CREATE TABLE IF NOT EXISTS central_outbox (
                    edge_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
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
            """)
            db.execute("PRAGMA optimize")

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
            collar = dict(db.execute("SELECT * FROM collars WHERE collar_id = ?", (packet.collar_id,)).fetchone())
            return collar, event

    def list_collars(self):
        with self._lock, self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM collars ORDER BY collar_id").fetchall()]

    def get_collar(self, collar_id: str):
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM collars WHERE collar_id = ?", (collar_id,)).fetchone()
            return dict(row) if row else None

    def telemetry(self, collar_id: str, limit: int = 100):
        with self._lock, self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM telemetry WHERE collar_id = ? ORDER BY timestamp DESC LIMIT ?",
                (collar_id, limit)).fetchall()]

    def events(self, limit: int = 100):
        with self._lock, self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]

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
                """INSERT INTO fence_deliveries(version,collar_id,status,detail,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(version,collar_id) DO UPDATE SET
                   status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at""",
                (version, collar_id, status, detail, timestamp),
            )
            return self._queue(db, "delivery", data, timestamp)

    def outbox_batch(self, limit: int = 100) -> list[dict]:
        with self._lock, self.connect() as db:
            rows = db.execute(
                "SELECT edge_seq,item_type,data_json FROM central_outbox ORDER BY edge_seq LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"edge_seq": row["edge_seq"], "type": row["item_type"],
                 "data": json.loads(row["data_json"])}
                for row in rows
            ]

    def acknowledge_outbox(self, edge_seq: int) -> int:
        with self._lock, self.connect() as db:
            cursor = db.execute("DELETE FROM central_outbox WHERE edge_seq <= ?", (edge_seq,))
            return cursor.rowcount

    def outbox_size(self) -> int:
        with self._lock, self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM central_outbox").fetchone()[0])

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
