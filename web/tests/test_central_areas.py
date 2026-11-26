from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import create_app
from backend.security import SESSION_COOKIE, create_session_token
from backend.services import delivery, fence_log


def publish(client, data, *, path="/api/fences", key=None):
    return client.post(path, json=data, headers={"Idempotency-Key": key} if key else {})


def push(client, headers, seq, kind, data):
    return client.post("/api/edge/batch", headers=headers, json={
        "base_id": "BASE01", "generation": "a" * 32,
        "items": [{"edge_seq": seq, "type": kind, "data": data}],
    })


def test_delayed_telemetry_and_offline_event_do_not_replace_current_and_bad_stamp_has_no_ack(
    client, base_headers,
):
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    late = recent - timedelta(hours=1)
    pos = {"collar_id": "COL01", "lat": -31.1, "lon": -54, "zone": "SEGURO", "ts": recent.isoformat()}
    assert push(client, base_headers, 1, "telemetry", pos).json()["acked_sequences"] == [1]
    old = {**pos, "lat": -31.2, "zone": "FORA", "ts": late.astimezone(timezone(timedelta(hours=-3))).isoformat()}
    assert push(client, base_headers, 2, "telemetry", old).status_code == 200
    assert push(client, base_headers, 3, "event",
                {"collar_id": "COL01", "kind": "collar_offline", "ts": late.isoformat()}).status_code == 200
    denied = push(client, base_headers, 4, "telemetry", {**pos, "ts": "invalid-stamp"})
    assert denied.status_code == 422 and "acked_sequences" not in denied.json()
    assert push(client, base_headers, 4, "telemetry", pos).json()["acked_sequences"] == [4]
    response = client.get("/api/collars").json()[0]
    assert response["last_lat"] == -31.1 and response["last_zone"] == "SEGURO"
    assert response["link_status"] == "online" and not response["position_stale"]
    assert response["received_at"] != response["last_contact_at"]
    assert any(row["lat"] == -31.2 and row["ts"].endswith("Z")
               for row in client.get("/api/collars/COL01/telemetry").json())


@pytest.mark.parametrize("module, operation", [(delivery, "create_pending"), (fence_log, "record_validation")])
def test_publication_rolls_back_after_points_and_keeps_last_active(
    app, client, settings, cerca_limpa, monkeypatch, module, operation,
):
    first = publish(client, cerca_limpa).json()
    def broken(*args, **kwargs):
        raise RuntimeError("test write failure")
    with monkeypatch.context() as isolated:
        isolated.setattr(module, operation, broken)
        with TestClient(app, raise_server_exceptions=False) as requester:
            requester.post("/api/auth/login", json={"user": settings.admin_user, "password": settings.admin_password})
            assert publish(requester, {**cerca_limpa, "name": "should not exist"}).status_code == 500
    assert client.get("/api/fences/active").json()["version"] == first["version"]
    assert len(client.get("/api/fences").json()) == 1
    assert len(client.get("/api/fences/1/deliveries").json()) == len(settings.known_collars)


def test_repeating_a_concurrent_publication_has_one_version_and_rejects_new_body(client, cerca_limpa):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as threads:
        responses = list(threads.map(lambda _: publish(client, cerca_limpa, key=key), range(2)))
    assert [r.status_code for r in responses] == [201, 201]
    assert [r.json()["version"] for r in responses] == [1, 1]
    assert publish(client, {**cerca_limpa, "name": "Other fence"}, key=key).status_code == 409
    assert len(client.get("/api/fences").json()) == 1


@pytest.mark.parametrize(("table", "action"), [
    ("fences", "UPDATE"), ("fences", "INSERT"), ("fence_points", "INSERT"), ("deliveries", "INSERT"),
])
def test_sqlite_rolls_back_each_failed_publication_write(
    app, client, settings, cerca_limpa, table, action,
):
    first = publish(client, cerca_limpa).json()["version"]
    with app.state.db.connect() as conn:
        conn.execute(
            f"CREATE TRIGGER fail_audit BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
        )
    with TestClient(app, raise_server_exceptions=False) as requester:
        requester.cookies.update(client.cookies)
        assert publish(requester, {**cerca_limpa, "name": "Must roll back"}).status_code == 500
    assert client.get("/api/fences/active").json()["version"] == first
    assert len(client.get("/api/fences").json()) == 1
    assert len(client.get("/api/fences/1/deliveries").json()) == len(settings.known_collars)


def test_visitor_empty_and_read_only_and_only_gets_individually_approved_fields(client):
    client_anonimo = TestClient(client.app)
    assert client_anonimo.get("/visitante").status_code == 200
    assert client_anonimo.get("/api/public/rebanho").json()["animals"] == []
    for page in ("/produtor", "/gestao"):
        response = client_anonimo.get(page, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login?next=")
    assert client_anonimo.put("/api/public/rebanho", json={}).status_code == 405
    assert client_anonimo.post("/api/producer/fences", json={}).status_code == 401
    assert client_anonimo.put("/api/gestao/publicacao", json={}).status_code == 401
    assert client_anonimo.get("/api/collars").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client_anonimo.websocket_connect("/ws/producer"):
            pass
    response = client.put("/api/gestao/publicacao", json={"animals": [{"collar_id": "COL01", "name": "Pampa"}]})
    assert response.status_code == 200
    readings = client_anonimo.get("/api/public/rebanho")
    assert readings.headers["cache-control"] == "no-store"
    assert readings.json()["animals"] == [{
        "key": "Pampa", "name": "Pampa", "status": None, "position": None,
        "alerts": [], "last_position_at": None,
    }]


def test_distinct_producer_uses_same_editor_scoped_api_and_cannot_use_admin_or_bearer(
    settings, cerca_limpa,
):
    settings = replace(settings, producer_user="limited-producer", producer_password="test-producer-only")
    app = create_app(settings=settings, log_to_file=False)
    with TestClient(app, follow_redirects=False) as c:
        login = c.post("/api/auth/login", json={"user": settings.producer_user, "password": settings.producer_password})
        assert login.status_code == 200
        assert c.get("/produtor/editor").status_code == 200
        assert c.get("/gestao").status_code == 303
        assert c.get("/api/collars").status_code == 403
        assert c.put("/api/gestao/publicacao", json={}).status_code == 403
        assert c.post("/api/edge/heartbeat", json={"base_id": "BASE01"}).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws"):
                pass
        with c.websocket_connect("/ws/producer") as socket:
            assert socket.receive_json() == {"type": "invalidate"}
        draft_id = str(uuid.uuid4())
        draft_path = "/api/producer/drafts/" + draft_id
        assert c.put(draft_path, json=cerca_limpa).status_code == 200
        assert c.get(draft_path).json()["points"] == cerca_limpa["points"]
        assert c.get("/api/producer/fences/active").status_code == 404
        r = publish(c, cerca_limpa, path="/api/producer/fences")
        assert r.status_code == 201 and "crc32" not in r.json()
        assert c.get("/api/producer/fences/1/deliveries").json()[0]["status"] == "pending"
        assert c.get("/produtor/editor?version=1").status_code == 200
        c.post("/api/auth/logout")
        assert c.get(draft_path).status_code == 401
        c.cookies.set(SESSION_COOKIE, create_session_token(settings.producer_user, settings.session_secret, -5))
        assert c.get(draft_path).status_code == 401
        c.cookies.clear()
        assert c.post("/api/auth/login", json={"user": settings.admin_user, "password": settings.admin_password}).status_code == 200
        management = c.get("/gestao/editor").text
        producer = c.get("/produtor/editor").text
        for page in (management, producer):
            for control in ("editor-map", "margin-attention", "margin-critical", "save-draft", "send-fence"):
                assert f'id="{control}"' in page
            assert 'src="/js/editor.js"' in page
        assert 'id="producer-versions"' in producer and 'id="producer-versions"' not in management
        assert 'href="/produtor' not in management
        assert 'href="/gestao' not in producer
        assert c.get("/api/producer/drafts").json() == []


def test_recovery_resolves_current_notice_and_keeps_history(client, base_headers):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=10)
    sample = {"collar_id": "COL01", "ts": stamp.isoformat(), "lat": -31.1, "lon": -54.2, "zone": "FORA"}
    assert push(client, base_headers, 1, "telemetry", sample).status_code == 200
    assert "Saiu da cerca" in client.get("/api/producer/rebanho").json()["animals"][0]["alerts"]
    recovered = {"collar_id": "COL01", "ts": (stamp + timedelta(seconds=2)).isoformat(), "kind": "back_inside"}
    for seq in (2, 3):
        assert push(client, base_headers, seq, "event", recovered).status_code == 200
    snapshot = client.get("/api/producer/rebanho").json()
    assert snapshot["animals"][0]["alerts"] == []
    assert snapshot["animals"][0]["key"] != "COL01"
    assert len(snapshot["history"]) == 1 and snapshot["history"][0]["resolved"]


def test_public_map_rounding_and_fence_opt_in_do_not_leak_private_data(client, base_headers, cerca_limpa):
    now = datetime.now(timezone.utc).isoformat()
    pos = {"collar_id": "COL01", "ts": now, "lat": -31.306119, "lon": -54.063935, "zone": "SEGURO",
           "rssi": -89, "hdop": 1.3}
    assert push(client, base_headers, 1, "telemetry", pos).status_code == 200
    assert publish(client, cerca_limpa).status_code == 201
    config = {"animals": [{"collar_id": "COL01", "name": "Pampa", "decimals": 3}], "fence_versions": [1]}
    assert client.put("/api/gestao/publicacao", json=config).status_code == 200
    public = TestClient(client.app).get("/api/public/rebanho").json()
    assert public["animals"][0]["position"] == {"lat": -31.306, "lon": -54.064, "current": True}
    assert public["fences"] == [{"points": cerca_limpa["points"]}]
    assert not any(private in str(public) for private in ("COL01", "COL02", "BASE01", "rssi", "hdop", "crc32"))
    assert client.put("/api/gestao/publicacao", json={}).status_code == 200
    assert TestClient(client.app).get("/api/public/rebanho").json()["fences"] == []


def test_public_status_distinguishes_current_stale_no_fix_and_offline(client, base_headers):
    now = datetime.now(timezone.utc) - timedelta(seconds=10)
    sample = {"collar_id": "COL01", "ts": (now - timedelta(seconds=130)).isoformat(),
              "lat": -31.3061, "lon": -54.0639, "zone": "FORA"}
    assert push(client, base_headers, 1, "telemetry", sample).status_code == 200
    policy = {"animals": [{"collar_id": "COL01", "name": "Pampa", "status": True,
                           "alerts": True, "decimals": 5}]}
    assert client.put("/api/gestao/publicacao", json=policy).status_code == 200
    visitor = TestClient(client.app)

    def public_animal():
        return visitor.get("/api/public/rebanho").json()["animals"][0]

    stale = public_animal()
    assert (stale["link"], stale["location_state"], stale["position"]) == ("online", "stale", None)
    assert stale["last_position_at"] is None
    assert push(client, base_headers, 2, "event", {"collar_id": "COL01", "kind": "gps_lost",
                                                  "ts": now.isoformat()}).status_code == 200
    assert public_animal()["location_state"] == "no_fix"
    sample.update(ts=(now + timedelta(seconds=1)).isoformat())
    assert push(client, base_headers, 3, "telemetry", sample).status_code == 200
    current = public_animal()
    assert current["status"] == "outside" and current["position"]["current"]
    assert current["alerts"] == ["Saiu da cerca"]
    assert push(client, base_headers, 4, "event", {"collar_id": "COL01", "kind": "collar_offline",
                                                  "ts": (now + timedelta(seconds=2)).isoformat()}).status_code == 200
    offline = public_animal()
    assert offline["link"] == offline["status"] == "offline" and offline["position"] is None
    assert offline["alerts"] == ["Coleira sem comunicação"]


def test_legacy_confirmation_is_reported_without_claiming_physical_origin(client, base_headers, cerca_limpa):
    fence = publish(client, cerca_limpa).json()
    assert client.get("/api/edge/fences/1", headers=base_headers).json()["crc32"] == fence["crc32"]
    report = {"collar_id": "COL01", "fence_version": 1, "status": "confirmed", "crc32": fence["crc32"]}
    assert push(client, base_headers, 1, "delivery", report).json()["acked_sequences"] == [1]
    assert push(client, base_headers, 1, "delivery", report).json()["acked_sequences"] == [1]
    row = client.get("/api/producer/fences/1/deliveries").json()[0]
    assert row["status"] == "confirmed" and row["evidence"] == "unverified"
    assert "confirm" in row["status_label"].lower()
    assert "crc32" not in row


def test_area_navigation_shares_rebanho_and_exposes_only_allowed_actions(client):
    visitor_client = TestClient(client.app)
    visitor = visitor_client.get("/visitante").text
    producer = client.get("/produtor").text
    management = client.get("/gestao").text
    fences = client.get("/produtor/cercas").text

    for content in (visitor, producer, management):
        assert 'id="shared-herd-map"' in content
    for content in (visitor, management):
        assert 'data-back href="/"' in content
    assert 'Criar cerca' not in visitor and 'id="logout-button"' not in visitor
    for content in (producer, fences):
        assert 'href="/produtor"' in content and 'href="/produtor/cercas"' in content
        assert 'href="/produtor/avisos"' not in content and 'href="/gestao' not in content
        assert 'id="logout-button"' in content
    assert client.get("/produtor/avisos", follow_redirects=False).headers["location"] == "/produtor#visitor-alerts-title"
    assert 'href="/produtor" aria-current="page"' in producer
    assert 'href="/produtor/cercas" aria-current="page"' in fences
    assert 'href="/gestao/editor"' in management and 'href="/produtor' not in management
    assert 'id="editor-map"' in client.get("/produtor/editor").text
    assert 'data-back href="/gestao/cercas"' in client.get("/gestao/editor").text
    assert 'data-back href="/"' in visitor_client.get("/login").text
