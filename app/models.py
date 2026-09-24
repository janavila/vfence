from dataclasses import dataclass


@dataclass(slots=True)
class GatewayState:
    status: str = "offline"
    serial_port: str = ""
    last_message: str | None = None
    last_seen: str | None = None
    error: str | None = None
