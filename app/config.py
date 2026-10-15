from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("VFENCE_HOST", "0.0.0.0")
    port: int = int(os.getenv("VFENCE_PORT", "8000"))
    serial_port: str = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
    serial_baud: int = int(os.getenv("SERIAL_BAUD", "115200"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./vfence.db")
    mock_serial: bool = _as_bool(os.getenv("VFENCE_MOCK_SERIAL"), False)
    reconnect_seconds: float = float(os.getenv("SERIAL_RECONNECT_SECONDS", "3"))
    collar_offline_seconds: int = int(os.getenv("COLLAR_OFFLINE_SECONDS", "30"))
    max_line_length: int = int(os.getenv("MAX_LINE_LENGTH", "512"))
    mock_interval_seconds: float = float(os.getenv("MOCK_INTERVAL_SECONDS", "3"))
    central_sync_enabled: bool = _as_bool(os.getenv("CENTRAL_SYNC_ENABLED"), False)
    central_base_url: str = os.getenv("CENTRAL_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    base_id: str = os.getenv("BASE_ID", "BASE01")
    base_token: str = os.getenv("BASE_TOKEN", "")
    central_timeout_seconds: float = float(os.getenv("CENTRAL_TIMEOUT_SECONDS", "10"))
    central_sync_interval_seconds: float = float(os.getenv("CENTRAL_SYNC_INTERVAL_SECONDS", "5"))
    central_retry_seconds: float = float(os.getenv("CENTRAL_RETRY_SECONDS", "5"))
    central_batch_size: int = int(os.getenv("CENTRAL_BATCH_SIZE", "100"))
    known_collar_ids: tuple[str, ...] = tuple(
        item.strip().upper()
        for item in os.getenv("COLLAR_IDS", "COL01,COL02").split(",")
        if item.strip()
    )
    collar_telemetry_seconds: int = int(os.getenv("COLLAR_TELEMETRY_SECONDS", "10"))
    collar_gps_timeout_seconds: int = int(os.getenv("COLLAR_GPS_TIMEOUT_SECONDS", "60"))
    collar_low_battery_pct: int = int(os.getenv("COLLAR_LOW_BATTERY_PCT", "20"))
    collar_critical_battery_pct: int = int(os.getenv("COLLAR_CRITICAL_BATTERY_PCT", "5"))


settings = Settings()
