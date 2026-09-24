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


settings = Settings()
