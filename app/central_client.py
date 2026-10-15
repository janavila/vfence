from __future__ import annotations

import struct
import zlib

import httpx


class CentralClientError(RuntimeError):
    pass


class CentralAuthenticationError(CentralClientError):
    pass


class InvalidFenceError(CentralClientError):
    pass


def canonical_fence_bytes(fence: dict) -> bytes:
    points = fence["points_e6"]
    blob = struct.pack(
        "<IHHB",
        int(fence["version"]),
        int(fence["margin_attention_cm"]),
        int(fence["margin_critical_cm"]),
        len(points),
    )
    for latitude_e6, longitude_e6 in points:
        blob += struct.pack("<ii", int(latitude_e6), int(longitude_e6))
    return blob


def central_fence_crc32(fence: dict) -> str:
    return f"{zlib.crc32(canonical_fence_bytes(fence)):08X}"


def validate_central_fence(fence: dict) -> dict:
    required = {
        "version", "margin_attention_cm", "margin_critical_cm",
        "points_e6", "crc32",
    }
    if not isinstance(fence, dict) or not required.issubset(fence):
        raise InvalidFenceError("resposta de cerca incompleta")
    version = fence["version"]
    if not isinstance(version, int) or isinstance(version, bool) or not 0 <= version <= 65535:
        raise InvalidFenceError("versão incompatível com uint16 do gateway")
    points = fence["points_e6"]
    if not isinstance(points, list) or not 3 <= len(points) <= 32:
        raise InvalidFenceError("cerca deve conter entre 3 e 32 pontos")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in point)):
            raise InvalidFenceError("ponto E6 inválido")
        if not -90_000_000 <= point[0] <= 90_000_000:
            raise InvalidFenceError("latitude E6 fora do intervalo")
        if not -180_000_000 <= point[1] <= 180_000_000:
            raise InvalidFenceError("longitude E6 fora do intervalo")
    expected = central_fence_crc32(fence)
    received = str(fence["crc32"]).upper()
    if received != expected:
        raise InvalidFenceError(f"CRC Central inválido: esperado {expected}, recebido {received}")
    fence = dict(fence)
    fence["crc32"] = received
    return fence


class CentralClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, headers=self.headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CentralClientError(str(exc)) from exc
        if response.status_code in {401, 403}:
            raise CentralAuthenticationError(response.text)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CentralClientError(
                f"Central respondeu HTTP {response.status_code}: {response.text[:200]}"
            ) from exc
        return response

    async def heartbeat(self, *, base_id: str, fence_version: int | None,
                        serial_status: str, queue_size: int) -> dict:
        response = await self._request("POST", "/api/edge/heartbeat", json={
            "base_id": base_id,
            "fence_version": fence_version,
            "serial": serial_status,
            "queue_size": queue_size,
        })
        data = response.json()
        if "desired_fence_version" not in data or "server_time" not in data:
            raise CentralClientError("resposta de heartbeat inválida")
        return data

    async def download_fence(self, version: int) -> dict:
        response = await self._request("GET", f"/api/edge/fences/{version}")
        try:
            return validate_central_fence(response.json())
        except (ValueError, TypeError) as exc:
            raise InvalidFenceError("JSON de cerca inválido") from exc

    async def send_batch(self, base_id: str, items: list[dict]) -> int:
        response = await self._request(
            "POST", "/api/edge/batch", json={"base_id": base_id, "items": items}
        )
        data = response.json()
        acked = data.get("acked_up_to")
        if not isinstance(acked, int):
            raise CentralClientError("resposta de lote inválida")
        return acked
