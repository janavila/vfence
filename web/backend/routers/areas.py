from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.clock import utc_now_iso
from backend.routers import fences, monitoring
from backend.schemas.fences import FenceIn, PointIn
from backend.services import delivery, edge_service, fence_service

router = APIRouter()
ZONE_STATUS = {"SEGURO": "inside", "ATENCAO": "near", "CRITICO": "near", "FORA": "outside"}
EVENT_TEXT = {
    "outside": "Saiu da cerca", "back_inside": "Voltou para dentro da cerca",
    "gps_lost": "Localização indisponível", "gps_recovered": "Localização recuperada",
    "collar_offline": "Coleira sem comunicação", "collar_online": "Coleira voltou a comunicar",
    "offline": "Coleira sem comunicação",
}
ZONE_NOTICES = {"FORA": "Saiu da cerca", "ATENCAO": "Perto da cerca", "CRITICO": "Muito perto da cerca"}
DELIVERY_TEXT = {
    "pending": "Aguardando a Base", "at_base": "Download solicitado pela Base",
    "transmitting": "Enviando à coleira", "confirmed": "Confirmação informada pela Base",
    "failed": "Entrega falhou",
}
DELIVERY_FAILURES = {
    "gateway_confirmation_timeout": "A confirmação não chegou no prazo.",
    "crc_missing": "A confirmação chegou sem a verificação de integridade.",
    "crc_mismatch": "A verificação de integridade não corresponde à cerca publicada.",
}


class PublicAnimal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collar_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    decimals: int | None = Field(default=None, ge=0, le=7, strict=True)
    status: bool = False
    alerts: bool = False
    timestamps: bool = False


class PublicPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    animals: list[PublicAnimal] = Field(default_factory=list, max_length=254)
    fence_versions: list[Annotated[int, Field(ge=1, strict=True)]] = Field(default_factory=list, max_length=32)


class FenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="", max_length=80)
    margin_attention_m: float = Field(ge=0)
    margin_critical_m: float = Field(ge=0)
    points: list[PointIn] = Field(default_factory=list, max_length=32)
    source_version: int | None = Field(default=None, ge=1)


def context(request):
    return request.app.state.db, request.app.state.property_id or request.app.state.settings.base_id


def load_preference(db, scope, name):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM central_preferences WHERE scope=? AND name=?", (scope, name),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def save_preference(db, scope, name, payload):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO central_preferences (scope,name,payload) VALUES (?,?,?)
               ON CONFLICT(scope,name) DO UPDATE SET payload=excluded.payload""",
            (scope, name, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )


def draft_context(request):
    db, scope = context(request)
    return db, f"draft:{scope}:{request.state.user}"


def fence_view(fence, *, detailed=False):
    fields = ("version", "name", "status", "created_at", "margin_attention_m", "margin_critical_m",
              "point_count", "area_ha", "delivery_summary")
    result = {key: fence[key] for key in fields if key in fence}
    if detailed:
        result["points"] = [{"lat": p["lat"], "lon": p["lon"]} for p in fence["points"]]
    return result


def delivery_view(rows, version, names):
    return [{
        "name": names.get(row["collar_id"]) or "Animal",
        "status": row["status"], "status_label": DELIVERY_TEXT.get(row["status"], "Aguardando atualização"),
        "updated_at": row["updated_at"],
        "detail": DELIVERY_FAILURES.get(row["detail"], "A Central recebeu um aviso de falha.")
        if row["status"] == "failed" else None,
        "evidence": "unverified" if row["status"] == "confirmed" else None,
        "confirmed_version": version if row["status"] == "confirmed" else None,
    } for row in rows]


def location_state(row, current):
    if current:
        return "current"
    if row["gnss_status"] == "invalid":
        return "no_fix"
    if row["position_stale"] and row["last_position_at"]:
        return "stale"
    return "unavailable"


def animal_view(row, active_version, ordinal):
    link = row["link_status"] or "unknown"
    current = (link == "online" and not row["position_stale"] and row["gnss_status"] == "valid"
               and row["last_lat"] is not None and row["last_lon"] is not None)
    status = ZONE_STATUS.get(row["last_zone"], "unknown") if current else (
        "offline" if link == "offline" else "no_position" if link == "online" else "unknown")
    name = row["animal_label"] or f"Animal {ordinal}"
    alerts = []
    if status == "offline":
        alerts.append("Coleira sem comunicação")
    elif status == "no_position":
        alerts.append("Localização indisponível")
    elif current and row["last_zone"] in ZONE_NOTICES:
        alerts.append(ZONE_NOTICES[row["last_zone"]])
    if active_version is not None and active_version != row["confirmed_fence_version"]:
        alerts.append("Atualização de cerca pendente")
    return {
        "key": row["collar_id"], "name": name, "status": status, "link": link,
        "gnss": row["gnss_status"], "last_contact_at": row["last_contact_at"],
        "last_position_at": row["last_position_at"], "contact_age_s": row["contact_age_s"],
        "position_age_s": row["position_age_s"], "fence_used": row["fence_version_reported"],
        "location_state": location_state(row, current),
        "position": {"lat": row["last_lat"], "lon": row["last_lon"], "current": current}
        if row["last_lat"] is not None and row["last_lon"] is not None else None,
        "alerts": alerts,
    }


def herd_snapshot(db):
    with db.connect() as conn:
        active = fence_service.get_active(db)
        version = active["version"] if active else None
        rows = monitoring._collars(db, version)
        animals = [animal_view(row, version, ordinal) for ordinal, row in enumerate(rows, start=1)]
        deliveries = delivery.list_for_fence(db, version) if version is not None else []
        by_recipient = {row["collar_id"]: row["status"] for row in deliveries}
        for animal in animals:
            animal["delivery_status"] = by_recipient.get(animal["key"])
        names = {animal["key"]: animal["name"] for animal in animals}
        by_key = {animal["key"]: animal for animal in animals}
        history_by_identity = {}
        records = conn.execute(
            "SELECT id,collar_id,kind,zone,ts FROM events ORDER BY id DESC LIMIT 100",
        )
        for record in records:
            kind = record["kind"]
            animal = by_key.get(record["collar_id"])
            label = ("Mudou de situação" if kind == "zone_change" else EVENT_TEXT.get(kind))
            if animal and label:
                identity = (animal["key"], kind, record["ts"], record["zone"])
                history_by_identity.setdefault(identity, {
                    "animal": animal["key"], "name": animal["name"], "message": label,
                    "at": record["ts"], "resolved": kind in {"back_inside", "gps_recovered", "collar_online"},
                })
        return {
            "animals": animals, "fences": [fence_view(active, detailed=True)] if active else [],
            "deliveries": delivery_view(deliveries, version, names),
            "history": sorted(history_by_identity.values(), key=lambda row: row["at"], reverse=True),
            "updated_at": max(
                [row["received_at"] for row in rows if row["received_at"]]
                + ([active["created_at"]] if active else []), default=None,
            ),
        }


def public_snapshot(db, scope):
    policy = PublicPolicy.model_validate(load_preference(db, scope, "public") or {})
    allowed = {entry.collar_id: entry for entry in policy.animals}
    animals = []
    for row in monitoring._collars(db, None) if allowed else ():
        entry = allowed.get(row["collar_id"])
        if not entry:
            continue
        animal = animal_view(row, None, 0)
        point = animal["position"]
        position = ({coord: round(point[coord], entry.decimals) for coord in ("lat", "lon")}
                    if point and point["current"] and entry.decimals is not None else None)
        public = {
            "key": entry.name, "name": entry.name,
            "status": animal["status"] if entry.status else None,
            "position": {**position, "current": True} if position is not None else None,
            "alerts": animal["alerts"] if entry.alerts else [],
            "last_position_at": animal["last_position_at"] if entry.timestamps else None,
        }
        if entry.status:
            public["link"] = animal["link"]
            public["location_state"] = animal["location_state"]
        animals.append(public)
    polygons = []
    for version in policy.fence_versions:
        try:
            fence = fence_service.get_by_version(db, version)
        except fence_service.FenceNotFound:
            continue
        polygons.append({"points": [{"lat": p["lat"], "lon": p["lon"]} for p in fence["points"]]})
    stamps = [animal["last_position_at"] for animal in animals if animal["last_position_at"]]
    return {"animals": animals, "fences": polygons, "updated_at": max(stamps, default=None)}


@router.get("/api/public/rebanho")
async def public_herd(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(public_snapshot, *context(request))


@router.get("/api/gestao/publicacao")
async def public_policy(request: Request):
    db, scope = context(request)
    return await asyncio.to_thread(load_preference, db, scope, "public") or PublicPolicy()


@router.put("/api/gestao/publicacao")
async def update_public_policy(data: PublicPolicy, request: Request):
    db, scope = context(request)
    collars = {r["collar_id"] for r in await asyncio.to_thread(db.list_collars)}
    ids = [entry.collar_id for entry in data.animals]
    if not set(ids) <= collars or len(set(ids)) != len(ids) or len(set(data.fence_versions)) != len(data.fence_versions):
        raise HTTPException(422, "invalid_public_selection")
    if any(not entry.name.strip() for entry in data.animals):
        raise HTTPException(422, "invalid_public_name")
    if len({entry.name.strip().casefold() for entry in data.animals}) != len(data.animals):
        raise HTTPException(422, "duplicate_public_name")
    for version in data.fence_versions:
        await fences._ensure_exists(db, version)
    await asyncio.to_thread(save_preference, db, scope, "public", data.model_dump())
    return data


@router.get("/api/producer/rebanho")
@router.get("/api/gestao/rebanho")
async def producer_herd(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    db, scope = context(request)
    snapshot = await asyncio.to_thread(herd_snapshot, db)
    settings = request.app.state.settings
    base = await asyncio.to_thread(db.get_base, settings.base_id)
    snapshot["base_online"] = bool(base and edge_service.base_is_online(base, settings))
    secret = settings.session_secret.encode()
    keys = {}
    for animal in snapshot["animals"]:
        original = animal["key"]
        if request.url.path == "/api/gestao/rebanho":
            animal["collar_id"] = original
        keys[original] = hmac.digest(secret, f"herd:{scope}:{original}".encode(), "sha256").hex()[:24]
        animal["key"] = keys[original]
    for event in snapshot["history"]:
        event["animal"] = keys[event["animal"]]
    return snapshot


@router.get("/api/producer/settings")
async def producer_settings(request: Request):
    settings = request.app.state.settings
    db, _ = context(request)
    config = {key: getattr(settings, key) for key in (
        "base_lat", "base_lon", "margin_attention_default_m", "margin_critical_default_m",
        "base_max_radius_km", "lora_range_m", "recommended_critical_margin_m", "gnss_expected_error_m",
    )}
    collars, base = await asyncio.gather(
        asyncio.to_thread(db.list_collars), asyncio.to_thread(db.get_base, settings.base_id),
    )
    config["known_collar_count"] = len(collars)
    config["base_online"] = bool(base and edge_service.base_is_online(base, settings))
    return config


@router.get("/api/producer/fences")
async def producer_fences(request: Request):
    rows = await asyncio.to_thread(fence_service.list_summaries, context(request)[0])
    return [fence_view(row) for row in rows]


@router.get("/api/producer/fences/active")
async def producer_active_fence(request: Request):
    fence = await fences.get_active_route(request)
    return fence_view(fence, detailed=True)


@router.post("/api/producer/fences/validate")
async def producer_validate(data: FenceIn, request: Request):
    return await fences.validate_fence_route(data, request)


@router.post("/api/producer/fences", status_code=201)
async def producer_publish(data: FenceIn, request: Request):
    return fence_view(await fences.create_fence_route(data, request), detailed=True)


@router.get("/api/producer/fences/{version}")
async def producer_fence(version: int, request: Request):
    return fence_view(await fences.get_fence_route(version, request), detailed=True)


@router.post("/api/producer/fences/{version}/reactivate", status_code=201)
async def producer_reactivate(version: int, request: Request):
    return fence_view(await fences.reactivate_route(version, request), detailed=True)


@router.get("/api/producer/fences/{version}/deliveries")
async def producer_delivery(version: int, request: Request):
    db, _ = context(request)
    await fences._ensure_exists(db, version)
    collars, rows = await asyncio.gather(
        asyncio.to_thread(db.list_collars), asyncio.to_thread(delivery.list_for_fence, db, version),
    )
    names = {row["collar_id"]: row["animal_label"] or f"Animal {ordinal}"
             for ordinal, row in enumerate(sorted(collars, key=lambda row: row["collar_id"]), start=1)}
    return delivery_view(rows, version, names)


@router.get("/api/producer/drafts")
async def list_drafts(request: Request):
    db, scope = draft_context(request)

    def read():
        with db.connect() as conn:
            return [json.loads(row["payload"]) for row in conn.execute(
                "SELECT payload FROM central_preferences WHERE scope=? ORDER BY name", (scope,),
            )]
    return await asyncio.to_thread(read)


@router.put("/api/producer/drafts/{draft_id}")
async def save_draft(draft_id: uuid.UUID, data: FenceDraft, request: Request):
    db, scope = draft_context(request)
    if any(not (-90 <= p.lat <= 90 and -180 <= p.lon <= 180) for p in data.points):
        raise HTTPException(422, "invalid_draft_coordinates")
    if data.source_version is not None:
        await fences._ensure_exists(db, data.source_version)
    draft = {"id": str(draft_id), **data.model_dump(), "updated_at": utc_now_iso()}
    await asyncio.to_thread(save_preference, db, scope, str(draft_id), draft)
    return draft


@router.get("/api/producer/drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID, request: Request):
    db, scope = draft_context(request)
    draft = await asyncio.to_thread(load_preference, db, scope, str(draft_id))
    if draft is None:
        raise HTTPException(404, "draft_not_found")
    return draft
