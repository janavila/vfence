from __future__ import annotations

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
            """)
            db.execute("PRAGMA optimize")

    def save_packet(self, packet: ParsedPacket):
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
