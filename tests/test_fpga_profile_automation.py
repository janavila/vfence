from types import SimpleNamespace

import pytest

from app import profile_api
from app.database import Database
from app.fpga_window import FpgaWindow


def test_full_v2_group_fits_fpga_window():
    sessions = {collar: str(collar) for collar in range(239, 255)}
    assert len(FpgaWindow("gw", "session", 1, 0, sessions).collar_sessions) == 16
    with pytest.raises(ValueError):
        FpgaWindow("gw", "session", 1, 0, {**sessions, 1: "boot"})


async def test_start_rejects_id_different_from_allocated_operation(monkeypatch):
    session = "1234567890abcdef"
    database = Database("sqlite:///:memory:")
    database.initialize()
    current = dict(gatewaySessionId=session, operationId=0, activeProfileId=0)

    async def reply(_reader, command, **fields):
        assert command == "START_PROFILE_V2" and fields["operationId"] == 1
        return True, {**current, "operationId": 9, "sourceProfileId": 0, "targetProfileId": 1,
                      "state": "COMPLETED"}

    monkeypatch.setattr(profile_api, "_ask", reply)
    with pytest.raises(profile_api.HTTPException, match="profile_transition_identity_mismatch"):
        await profile_api._start_operation(None, database, current, 1, [2])
    assert database.profile_history(1)[0]["status"] == "REQUESTED"


@pytest.mark.parametrize("reported", [[7, 17], [2], [2, 2], [2, 17, 23], [17, 2]])
async def test_start_requires_exact_participant_roster(monkeypatch, reported):
    session = "1234567890abcdef"
    database = Database("sqlite:///:memory:")
    database.initialize()
    current = dict(gatewaySessionId=session, operationId=0, activeProfileId=0)

    async def reply(_reader, command, **fields):
        assert command == "START_PROFILE_V2" and fields["operationId"] == 1 and fields["roster"] == "2,17"
        return True, {**current, "operationId": 1, "sourceProfileId": 0, "targetProfileId": 1,
                      "state": "PREPARING", "participants": [{"collarId": cid, "proved": False} for cid in reported]}

    monkeypatch.setattr(profile_api, "_ask", reply)
    if reported == [17, 2]:
        result = await profile_api._start_operation(None, database, current, 1, [2, 17])
        assert result["operationId"] == 1 and database.profile_history(1)[0]["status"] == "PREPARING"
    else:
        with pytest.raises(profile_api.HTTPException, match="profile_transition_identity_mismatch"):
            await profile_api._start_operation(None, database, current, 1, [2, 17])
        assert database.profile_history(1)[0]["status"] == "REQUESTED"


@pytest.mark.parametrize("scenario", ["two_legs", "missing_proof", "rollback", "expired", "wrong_transaction", "disabled",
                                     "wrong_completed_source", "wrong_completed_target", "wrong_completed_active",
                                     "wrong_completed_session", "wrong_completed_operation", "wrong_completed_roster"])
async def test_auto_profile_requires_integral_proof_for_each_leg(monkeypatch, scenario):
    config = SimpleNamespace(fpga_auto_profile_enabled=scenario != "disabled", profile_control_enabled=True,
                             profile_control_token="test", mock_serial=False, known_collar_ids=("COL02", "COL17"))
    reader = SimpleNamespace(state=SimpleNamespace(status="online"))
    valid = True
    targets = []

    def report(profile, operation, phase="COMPLETED", second_proof=True):
        return dict(gatewaySessionId="gateway", operationId=operation, activeProfileId=profile,
                    state=phase, switchEnabled=True,
                    participants=[dict(collarId=2, proved=True), dict(collarId=17, proved=second_proof)])

    origin = report(1, 1)
    current = origin

    async def start(_reader, _database, before, target, roster):
        nonlocal current
        assert roster == [2, 17]
        targets.append(target)
        current = report(target, before["operationId"] + 1,
                         phase="ROLLING_BACK" if scenario == "rollback" else "COMPLETED",
                         second_proof=scenario != "missing_proof")
        current["sourceProfileId"] = before["activeProfileId"]
        current["targetProfileId"] = (target + 1) % 4 if scenario == "wrong_transaction" else target
        return current

    async def snapshot(*_):
        nonlocal valid
        if scenario == "expired" and current["operationId"] == 2:
            valid = False
        if current["operationId"] == 2:
            changes = {
                "wrong_completed_source": {"sourceProfileId": 3},
                "wrong_completed_target": {"targetProfileId": 3},
                "wrong_completed_active": {"activeProfileId": 3},
                "wrong_completed_session": {"gatewaySessionId": "other"},
                "wrong_completed_operation": {"operationId": 9},
                "wrong_completed_roster": {"participants": [{"collarId": 2, "proved": True},
                                                             {"collarId": 7, "proved": True}]},
            }
            return {**current, **changes.get(scenario, {})}
        return current

    async def still_valid():
        return valid

    async def pause():
        pytest.fail("terminal mock reports should not poll")

    monkeypatch.setattr(profile_api, "_snapshot", snapshot)
    monkeypatch.setattr(profile_api, "_start_operation", start)
    result = await profile_api.apply_fpga_profile(config, reader, None, origin, 2, still_valid, pause)
    if scenario == "two_legs":
        assert targets == [0, 2] and result["activeProfileId"] == 2
    else:
        assert result is None
        assert targets == ([] if scenario == "disabled" else [0])
