"""Bind each producer host and logical Base to an isolated Central datastore."""

from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Host

from backend.config import Settings

_SCOPE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


@dataclass(frozen=True, slots=True)
class Property:
    property_id: str
    hostname: str
    settings: Settings


def _read_env(entry: dict, key: str) -> str:
    name = entry.get(key)
    if not isinstance(name, str) or not _ENV.fullmatch(name) or not os.environ.get(name):
        raise ValueError(f"missing property environment reference: {key}")
    return os.environ[name]


def load_properties(path: Path, defaults: Settings) -> list[Property]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ValueError("property manifest must contain 1 to 64 entries")
    properties = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid property record")
        database_url = _read_env(entry, "database_url_env") if "database_url_env" in entry else None
        folder = entry.get("data_dir")
        if database_url is None and (not isinstance(folder, str) or not folder):
            raise ValueError("property needs explicit storage")
        data_dir = (path.parent / folder).resolve() if folder else defaults.data_dir
        collars = entry.get("known_collars", [])
        if not isinstance(collars, list) or any(not isinstance(value, str) or not _SCOPE.fullmatch(value)
                                               for value in collars):
            raise ValueError("invalid property collars")
        settings = replace(
            defaults, auth_enabled=True, data_dir=data_dir, database_url=database_url,
            base_id=entry["base_id"], base_token=_read_env(entry, "base_token_env"),
            admin_user=entry["admin_user"], admin_password=_read_env(entry, "admin_password_env"),
            session_secret=_read_env(entry, "session_secret_env"), known_collars=tuple(collars),
            producer_user=entry.get("producer_user", ""),
            producer_password=_read_env(entry, "producer_password_env") if "producer_password_env" in entry else "",
            base_lat=float(entry.get("base_lat", defaults.base_lat)),
            base_lon=float(entry.get("base_lon", defaults.base_lon)),
            property_timezone=entry.get("timezone", defaults.property_timezone),
        )
        if not (-90 <= settings.base_lat <= 90 and -180 <= settings.base_lon <= 180):
            raise ValueError("invalid property coordinates")
        if not isinstance(settings.property_timezone, str) or not settings.property_timezone.strip():
            raise ValueError("invalid property timezone")
        properties.append(Property(entry["property_id"], entry["hostname"], settings))
    return properties


def create_property_app(properties: list[Property], *, log_to_file: bool = False) -> Starlette:
    from backend.main import create_app
    from backend.config import _check

    if not properties:
        raise ValueError("no properties configured")
    for prop in properties:
        _check(prop.settings)
        if (not _SCOPE.fullmatch(prop.property_id) or not _HOST.fullmatch(prop.hostname) or
                not _SCOPE.fullmatch(prop.settings.base_id) or not prop.settings.auth_enabled or
                not prop.settings.admin_user or not prop.settings.admin_password or
                not prop.settings.session_secret or not prop.settings.base_token):
            raise ValueError("invalid property identity or authentication")
    for values in (
        [prop.property_id for prop in properties], [prop.hostname for prop in properties],
        [prop.settings.base_id for prop in properties], [prop.settings.admin_user for prop in properties],
        [prop.settings.base_token for prop in properties], [prop.settings.session_secret for prop in properties],
        [str(prop.settings.database_target) for prop in properties],
    ):
        if len(set(values)) != len(properties):
            raise ValueError("property configuration must isolate hosts, credentials and storage")

    apps = [create_app(settings=prop.settings, property_id=prop.property_id, log_to_file=log_to_file)
            for prop in properties]

    @asynccontextmanager
    async def lifespan(_):
        async with AsyncExitStack() as stack:
            for child in apps:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    return Starlette(routes=[Host(prop.hostname, app=child) for prop, child in zip(properties, apps)],
                     lifespan=lifespan)
