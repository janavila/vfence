from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.db import Database, DatabaseConfigurationError
from backend.properties import Property, create_property_app
from backend.security import SESSION_COOKIE, create_session_token


def scoped_settings(settings, tmp_path):
    first = replace(settings, data_dir=tmp_path / "first", known_collars=("COL01",))
    second = replace(first, data_dir=tmp_path / "second", base_id="BASE02",
                     base_token="example-property-two-token", admin_user="other-producer",
                     admin_password="other-test-password", session_secret="other-test-signing-secret")
    return [Property("first-farm", "first.test", first), Property("second-farm", "second.test", second)]


def cookies(settings):
    return {"Cookie": f"{SESSION_COOKIE}={create_session_token(settings.admin_user, settings.session_secret, 3600)}"}


def test_two_properties_have_independent_collars_fences_events_and_websocket(settings, tmp_path, cerca_limpa):
    first, second = scoped_settings(settings, tmp_path)
    app = create_property_app([first, second])
    properties = [(first, "outside", -31.1), (second, "gps_lost", -31.2)]
    with TestClient(app) as client:
        for prop, event, lat in properties:
            base, host = prop.settings, f"http://{prop.hostname}"
            other = second.settings if prop == first else first.settings
            headers = {"Authorization": f"Bearer {base.base_token}"}
            payload = {"base_id": base.base_id, "generation": "a" * 32, "items": [
                {"edge_seq": 1, "type": "telemetry", "data": {"collar_id": "COL01", "lat": lat, "lon": -54}},
                {"edge_seq": 2, "type": "event", "data": {"collar_id": "COL01", "kind": event}},
            ]}
            assert client.post(host + "/api/edge/batch", json=payload, headers=headers).json()["acked_sequences"] == [1, 2]
            assert client.post(host + "/api/edge/batch", json=payload,
                               headers={"Authorization": f"Bearer {other.base_token}"}).status_code == 401
            assert client.get(host + "/api/collars", headers=cookies(other)).status_code == 401
            own_session = cookies(base)
            created = client.post(host + "/api/fences", json={**cerca_limpa, "name": prop.property_id},
                                  headers=own_session)
            assert created.status_code == 201 and created.json()["version"] == 1
            assert client.get(host + "/api/fences/active", headers=own_session).json()["name"] == prop.property_id
            assert client.get(host + "/api/edge/fences/1", headers=headers).status_code == 200
            with client.websocket_connect(f"ws://{prop.hostname}/ws", headers=own_session) as websocket:
                hello = websocket.receive_json()
                assert hello["type"] == "hello"
                assert [row["base_id"] for row in hello["data"]["bases"]] == [base.base_id]
        for prop, event, lat in properties:
            host = f"http://{prop.hostname}"
            headers = cookies(prop.settings)
            rows = client.get(host + "/api/collars", headers=headers).json()
            assert [(row["collar_id"], row["base_id"], row["last_lat"]) for row in rows] == [
                ("COL01", prop.settings.base_id, lat),
            ]
            assert any(row["kind"] == event for row in client.get(host + "/api/events", headers=headers).json())
            assert client.get(host + "/api/collars/COL01/telemetry", headers=headers).json()[0]["lat"] == lat
        assert client.get("http://unknown.test/api/health").status_code == 404


def test_existing_store_can_be_bound_once_without_overwriting_records(settings, tmp_path):
    first, second = scoped_settings(settings, tmp_path)
    db = Database(first.settings.database_target)
    db.initialize()
    db.seed_from_settings(first.settings)
    db.bind_property(first.property_id, first.settings.base_id)
    db.bind_property(first.property_id, first.settings.base_id)
    assert db.list_collars()[0]["collar_id"] == "COL01"
    with pytest.raises(DatabaseConfigurationError, match="different owner"):
        db.bind_property(second.property_id, second.settings.base_id)
