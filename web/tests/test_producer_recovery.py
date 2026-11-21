import asyncio
import pytest

from backend.services.realtime import RealtimeHub


def test_current_gnss_history_and_label_remain_independent(client, base_headers):
    valid = {"collar_id": "COL01", "lat": -31, "lon": -54, "zone": "SEGURO", "ts": "2025-01-01T12:00:03Z"}
    invalid = {**valid, "lat": -1, "lon": -2, "zone": "GNSS_INVALIDO", "ts": "2025-01-01T12:00:05Z"}
    reported = client.post("/api/edge/batch", headers=base_headers, json={"base_id": "BASE01", "items": [
        {"edge_seq": 1, "type": "telemetry", "data": valid},
        {"edge_seq": 2, "type": "telemetry", "data": invalid},
        {"edge_seq": 3, "type": "event", "data": {"collar_id": "COL01", "kind": "collar_offline"}},
    ]})
    assert reported.status_code == 200
    changed = client.patch("/api/collars/COL01", json={"animal_label": "Pampa 12"})
    assert changed.status_code == 200
    animal = changed.json()
    assert animal["animal_label"] == "Pampa 12"
    assert (animal["last_lat"], animal["last_lon"], animal["last_position_at"]) == (-31, -54, valid["ts"])
    assert animal["position_stale"] and animal["gnss_status"] == "invalid" and animal["link_status"] == "offline"
    assert animal["last_contact_at"] == invalid["ts"]
    history = client.get("/api/collars/COL01/telemetry",
                         params={"end": "2025-01-01T12:00:04Z", "limit": 1}).json()
    assert len(history) == 1 and history[0]["ts"] == valid["ts"]
    assert client.get("/api/collars/COL01/telemetry", params={"before_id": history[0]["id"]}).json() == []
    assert client.get("/api/events", params={"collar_id": "COL01"}).json()[0]["kind"] == "collar_offline"


@pytest.mark.asyncio
async def test_central_broadcast_is_bounded_and_does_not_wait_for_slow_websocket(monkeypatch):
    from backend.services import realtime
    monkeypatch.setattr(realtime, "_QUEUE_SIZE", 2)
    monkeypatch.setattr(realtime, "_IO_TIMEOUT", 0.02)
    hub = RealtimeHub()
    started = asyncio.Event()

    class SlowSocket:
        def __init__(self):
            self.code = None

        async def accept(self):
            pass

        async def send_json(self, message):
            started.set()
            await asyncio.Event().wait()

        async def close(self, *, code):
            self.code = code

    slow = SlowSocket()
    assert await hub.connect(slow, {"type": "hello"})
    await asyncio.wait_for(started.wait(), 0.1)
    await asyncio.wait_for(hub.broadcast_many([("event", {"id": i}) for i in range(8)]), 0.01)
    await asyncio.sleep(0.05)
    assert hub.count == 0 and slow.code == 1013
