from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security import SESSION_COOKIE, create_session_token


def test_management_history_is_private_and_pages_a_complete_day(client, app, settings):
    assert client.get("/api/settings").json()["property_timezone"] == "America/Sao_Paulo"
    anonymous = TestClient(app)
    assert anonymous.get("/api/collars/COL01/telemetry").status_code == 401
    assert anonymous.get("/api/events/kinds").status_code == 401
    assert anonymous.get("/api/gestao/rebanho").status_code == 401
    start = datetime(2026, 6, 10, 3, tzinfo=timezone.utc)
    rows = [
        (settings.base_id, i, "COL01", (start + timedelta(seconds=i * 10)).isoformat(),
         -31.30, -54.06, "SEGURO", 1)
        for i in reversed(range(1005))
    ]
    with app.state.db.connect() as conn:
        conn.executemany(
            "INSERT INTO telemetry(base_id,edge_seq,collar_id,ts,lat,lon,zone,fence_version)"
            " VALUES (?,?,?,?,?,?,?,?)", rows,
        )
        conn.executemany("INSERT INTO events(collar_id,ts,kind) VALUES (?,?,?)", [
            ("COL01", start.isoformat(), "gps_lost"),
            ("COL01", start.isoformat(), "outside"),
        ])
    bounds = {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat(), "limit": 1000}
    first = client.get("/api/collars/COL01/telemetry", params=bounds).json()
    second = client.get("/api/collars/COL01/telemetry", params={**bounds, "before_id": first[-1]["id"]}).json()
    assert len(first) == 1000 and len(second) == 5
    assert len({row["id"] for row in first + second}) == 1005
    assert first[0]["ts"] == start.isoformat()
    events = client.get("/api/events", params={**bounds, "collar_id": "COL01", "kind": ["gps_lost", "offline"]})
    assert [row["kind"] for row in events.json()] == ["gps_lost"]
    assert set(client.get("/api/events/kinds").json()) == {"gps_lost", "outside"}
    assert "collar_id" not in client.get("/api/producer/rebanho").json()["animals"][0]
    assert client.get("/api/gestao/rebanho").json()["animals"][0]["collar_id"] == "COL01"
    assert client.get("/api/public/rebanho").json()["animals"] == []


def test_other_property_and_distinct_producer_cannot_read_management_history(settings, tmp_path):
    with TestClient(create_app(settings=replace(settings, data_dir=tmp_path / "other", known_collars=("OTHER",),
                                               producer_user="limited", producer_password="only-test-password"),
                               property_id="other", log_to_file=False)) as client:
        client.cookies.set(SESSION_COOKIE, create_session_token(settings.admin_user, settings.session_secret, 300))
        assert client.get("/api/collars/COL01/telemetry").status_code == 404
        client.cookies.set(SESSION_COOKIE, create_session_token("limited", settings.session_secret, 300))
        for route in ("/api/collars/OTHER/telemetry", "/api/events/kinds", "/api/gestao/rebanho"):
            assert client.get(route).status_code == 403


def test_public_visibility_has_compatible_link_and_editor_is_shared(client):
    response = client.get("/gestao/visitante", follow_redirects=False)
    assert response.headers["location"] == "/gestao/painel#visibilidade-publica"
    assert 'id="public-policy"' in client.get(response.headers["location"]).text
    for path in ("/produtor/cercas", "/gestao/cercas"):
        html = client.get(path).text
        assert 'src="/js/editor.js"' in html
        assert 'id="margin-attention" min="0.5" max="100" step="0.5" value="5"' in html
        assert 'id="margin-critical" min="0.1" max="100" step="0.5" value="2"' in html
