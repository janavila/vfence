from __future__ import annotations

import pytest


@pytest.mark.parametrize("generation", [None, "a" * 32])
def test_invalid_event_does_not_claim_identity_or_acknowledge_valid_neighbours(client, base_headers, database, generation):
    base = {"base_id": "BASE01"}
    if generation:
        base["generation"] = generation
    one = {"edge_seq": 1, "type": "event", "data": {"kind": "outside", "collar_id": "COL01"}}
    two = {"edge_seq": 2, "type": "event", "data": {"collar_id": "COL01"}}
    response = client.post("/api/edge/batch", headers=base_headers, json={**base, "items": [one, two]})
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "invalid_event_kind", "edge_seq": 2}
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM edge_receipts").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0
    two["data"]["kind"] = "back_inside"
    response = client.post("/api/edge/batch", headers=base_headers, json={**base, "items": [one, two]})
    assert response.status_code == 200
    expected = {"generation": generation, "acked_sequences": [1, 2]} if generation else {"acked_up_to": 2}
    assert response.json() == expected


def test_generation_and_payload_are_part_of_commit_identity(client, base_headers, database):
    bases = {"base_id": "BASE01", "generation": "b" * 32}

    def submit(event, generation=bases["generation"]):
        item = {"edge_seq": 9, "type": "event", "data": {"kind": event, "collar_id": "COL01"}}
        return client.post("/api/edge/batch", headers=base_headers, json={
            **bases, "generation": generation, "items": [item],
        })

    assert submit("outside").json() == {"generation": "b" * 32, "acked_sequences": [9]}
    assert submit("outside").status_code == 200
    conflict = submit("back_inside")
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "identity_conflict"
    assert submit("back_inside", "c" * 32).json() == {"generation": "c" * 32, "acked_sequences": [9]}
    with database.connect() as conn:
        events = conn.execute("SELECT kind FROM events ORDER BY id").fetchall()
    assert [row["kind"] for row in events] == ["outside", "back_inside"]


def test_generation_ack_only_lists_committed_members_even_when_sequences_have_a_gap(client, base_headers):
    result = client.post("/api/edge/batch", headers=base_headers, json={
        "base_id": "BASE01", "generation": "e" * 32,
        "items": [{"edge_seq": seq, "type": "event", "data": {"kind": "outside"}} for seq in (13, 15)],
    })
    assert result.json() == {"generation": "e" * 32, "acked_sequences": [13, 15]}
