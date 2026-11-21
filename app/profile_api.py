from __future__ import annotations

import asyncio
import re
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

router = APIRouter(prefix="/api/gateway/profile-control", tags=["profile-control"])
_states = {"IDLE", "PREPARING", "COMMITTING", "VERIFYING", "COMPLETED", "ROLLING_BACK", "FAILED"}
_operation_lock = asyncio.Lock()


class ProfileRequest(BaseModel):
    profile_id: int = Field(strict=True, ge=0, le=3)


class Participant(BaseModel):
    collarId: int = Field(strict=True, ge=1, le=254)
    proved: bool = Field(strict=True)


class ProfileReport(BaseModel):
    gatewaySessionId: str = Field(pattern=r"^[0-9a-f]{16}$")
    operationId: int = Field(strict=True, ge=0, le=0xFFFFFFFF)
    activeProfileId: int = Field(strict=True, ge=0, le=3)
    sourceProfileId: int = Field(strict=True, ge=0, le=3)
    targetProfileId: int = Field(strict=True, ge=0, le=3)
    deadlineAtMs: int = Field(strict=True, ge=0, le=0xFFFFFFFF)
    switchEnabled: bool = Field(strict=True)
    state: str
    participants: list[Participant] = Field(max_length=16)


def _service(token: str | None):
    from app import main

    settings = main.settings
    configured = getattr(settings, "profile_control_token", "")
    if not getattr(settings, "profile_control_enabled", False) or not configured:
        raise HTTPException(404, "profile_control_disabled")
    if not token or not secrets.compare_digest(token.encode(), configured.encode()):
        raise HTTPException(403, "profile_control_forbidden")
    if settings.mock_serial or main.reader.state.status != "online":
        raise HTTPException(503, "gateway_unavailable")
    return settings, main.reader, main.database


async def _ask(reader, command: str, **fields) -> tuple[bool, dict]:
    try:
        reply = await reader.send_gateway_command(command, **fields)
    except TimeoutError:
        raise HTTPException(504, "profile_command_outcome_unknown") from None
    except OSError:
        raise HTTPException(503, "gateway_unavailable") from None
    if reply.get("profileControlVersion") != 2 or reply.get("command") != command:
        raise HTTPException(501, "profile_control_v2_unsupported")
    try:
        report = ProfileReport.model_validate(reply).model_dump()
    except ValidationError:
        raise HTTPException(502, "invalid_profile_report") from None
    if report["state"] not in _states:
        raise HTTPException(502, "invalid_profile_report")
    return reply.get("ok") is True, report


async def _snapshot(reader, database):
    ok, report = await _ask(reader, "GET_PROFILE_V2")
    if not ok:
        raise HTTPException(502, "profile_report_refused")
    await asyncio.to_thread(database.record_profile_operation, report["gatewaySessionId"], report["operationId"], report)
    return report


@router.get("")
async def profile_status(x_vfence_profile_token: str | None = Header(None)):
    _, reader, database = _service(x_vfence_profile_token)
    return await _snapshot(reader, database)


@router.post("")
async def profile_start(body: ProfileRequest, x_vfence_profile_token: str | None = Header(None)):
    settings, reader, database = _service(x_vfence_profile_token)
    async with _operation_lock:
        current = await _snapshot(reader, database)
        if not current["switchEnabled"]:
            raise HTTPException(409, "profile_switch_disabled_at_gateway")
        if current["state"] in {"PREPARING", "COMMITTING", "VERIFYING", "ROLLING_BACK"}:
            raise HTTPException(409, "profile_transition_busy")
        known = []
        for name in settings.known_collar_ids:
            match = re.fullmatch(r"COL([0-9]{1,3})", name)
            if not match or not 1 <= int(match[1]) <= 254:
                raise HTTPException(409, "invalid_participant_configuration")
            known.append(int(match[1]))
        if not 1 <= len(known) <= 16 or len(set(known)) != len(known):
            raise HTTPException(409, "invalid_participant_configuration")
        session = current["gatewaySessionId"]
        operation = await asyncio.to_thread(database.start_profile_operation, session, current["operationId"],
                                            body.profile_id, sorted(known))
        ok, report = await _ask(reader, "START_PROFILE_V2", gatewaySessionId=session, operationId=operation,
                                profileId=body.profile_id, roster=",".join(map(str, sorted(known))))
        if not ok:
            await asyncio.to_thread(database.record_profile_operation, session, operation,
                                    {"state": "REFUSED"})
            raise HTTPException(409, "profile_transition_refused")
        await asyncio.to_thread(database.record_profile_operation, session, operation, report)
        return report


@router.post("/cancel")
async def profile_cancel(x_vfence_profile_token: str | None = Header(None)):
    _, reader, database = _service(x_vfence_profile_token)
    async with _operation_lock:
        current = await _snapshot(reader, database)
        ok, report = await _ask(reader, "CANCEL_PROFILE_V2", gatewaySessionId=current["gatewaySessionId"],
                                operationId=current["operationId"])
        if not ok:
            raise HTTPException(409, "profile_cancel_refused")
        await asyncio.to_thread(database.record_profile_operation, current["gatewaySessionId"],
                                current["operationId"], report)
        return report


@router.get("/history")
async def profile_history(x_vfence_profile_token: str | None = Header(None)):
    _, _, database = _service(x_vfence_profile_token)
    return await asyncio.to_thread(database.profile_history, 100)
