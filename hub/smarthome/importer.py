"""Discovery → registry: what a Provider account exposes becomes Owner devices in Data.

Data stays the only writer of the registry; this module calls its CAS write API.
The overlay rule: the Provider decides whether a device exists and what it is
(type, traits, limits); the Owner's hand edits (name, aliases, area) survive a
re-sync once they are listed in ``Device.overrides``. A device the Provider no
longer lists is not deleted but marked orphaned, so its names and placement
come back with it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from eidolon_sdk.biz.smarthome import (
    MAX_AREAS,
    MAX_DEVICES,
    Area,
    Device,
    DiscoveredDevice,
    Registry,
)

logger = logging.getLogger("hub.smarthome.importer")

NAME_MAX = 32


class RegistryWriter(Protocol):
    async def get(self, owner_id: str) -> Registry: ...

    async def create_area(self, owner_id: str, area: Area, expected_revision: int) -> Registry: ...

    async def create_device(
        self, owner_id: str, device: Device, expected_revision: int
    ) -> Registry: ...

    async def update_device(
        self, owner_id: str, device: Device, expected_revision: int
    ) -> Registry: ...


class HttpRegistryWriter:
    """Data's workspace-authority smart-home API, with the token Hub already holds."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str):
        self._client, self._base, self._token = client, base_url.rstrip("/"), token

    def _url(self, owner_id: str, resource: str) -> str:
        return (
            f"{self._base}/api/workspace-authority/v1/owners/{quote(owner_id, safe='')}"
            f"/smarthome/{resource}"
        )

    async def _call(self, method: str, owner_id: str, resource: str, payload: dict | None):
        response = await self._client.request(
            method,
            self._url(owner_id, resource),
            headers={"Authorization": f"Bearer {self._token}"},
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return Registry.model_validate(response.json())

    async def get(self, owner_id):
        return await self._call("GET", owner_id, "registry", None)

    async def create_area(self, owner_id, area, expected_revision):
        return await self._call(
            "POST",
            owner_id,
            "areas",
            {"expected_revision": expected_revision, "area": area.model_dump(mode="json")},
        )

    async def create_device(self, owner_id, device, expected_revision):
        return await self._call(
            "POST",
            owner_id,
            "devices",
            {"expected_revision": expected_revision, "device": device.model_dump(mode="json")},
        )

    async def update_device(self, owner_id, device, expected_revision):
        return await self._call(
            "PUT",
            owner_id,
            f"devices/{quote(device.device_id, safe='')}",
            {"expected_revision": expected_revision, "device": device.model_dump(mode="json")},
        )


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "orphaned": self.orphaned,
            "skipped": self.skipped,
            "revision": self.revision,
        }


class RegistryImporter:
    def __init__(self, writer: RegistryWriter, now_ms=None) -> None:
        self._writer = writer
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def sync(
        self, owner_id: str, provider: str, discovered: list[DiscoveredDevice]
    ) -> SyncReport:
        """Make Data's registry agree with ``discovered`` for devices of ``provider``."""
        report = SyncReport()
        registry = await self._writer.get(owner_id)
        now = self._now_ms()
        seen_refs: set[str] = set()
        for found in discovered:
            if found.external_ref in seen_refs:
                report.skipped.append({"ref": found.external_ref, "reason": "DUPLICATE_REF"})
                continue
            seen_refs.add(found.external_ref)
            registry, area_id = await self._ensure_area(owner_id, registry, found.area_name, report)
            current = next(
                (
                    d
                    for d in registry.devices
                    if d.provider == provider and d.provider_ref == found.external_ref
                ),
                None,
            )
            if current is None:
                if len(registry.devices) >= MAX_DEVICES:
                    report.skipped.append({"ref": found.external_ref, "reason": "LIMIT_EXCEEDED"})
                    continue
                device = Device(
                    device_id=_device_id(provider, found.external_ref),
                    name=_unique_name(registry, area_id, _clip_name(found.name)),
                    type=found.suggested_type,
                    area_id=area_id,
                    provider=provider,
                    provider_ref=found.external_ref,
                    traits=found.traits,
                    limits=found.limits,
                    source="imported",
                    synced_at_ms=now,
                )
                registry = await self._writer.create_device(owner_id, device, registry.revision)
                report.added.append(device.device_id)
                continue
            proposed = current.model_copy(
                update={
                    "type": found.suggested_type,
                    "traits": found.traits,
                    "limits": found.limits,
                    "synced_at_ms": now,
                    "orphaned": False,
                    "source": "imported",
                    **(
                        {}
                        if "name" in current.overrides
                        else {
                            "name": _unique_name(
                                registry,
                                area_id,
                                _clip_name(found.name),
                                except_id=current.device_id,
                            )
                        }
                    ),
                    **({} if "area_id" in current.overrides else {"area_id": area_id}),
                }
            )
            if _facts(proposed) != _facts(current):
                registry = await self._writer.update_device(owner_id, proposed, registry.revision)
                report.updated.append(current.device_id)
        for device in registry.devices:
            if (
                device.provider == provider
                and device.provider_ref not in seen_refs
                and not device.orphaned
            ):
                registry = await self._writer.update_device(
                    owner_id,
                    device.model_copy(update={"orphaned": True, "synced_at_ms": now}),
                    registry.revision,
                )
                report.orphaned.append(device.device_id)
        report.revision = registry.revision
        return report

    async def _ensure_area(
        self, owner_id: str, registry: Registry, area_name: str | None, report: SyncReport
    ) -> tuple[Registry, str | None]:
        if not area_name:
            return registry, None
        name = _clip_name(area_name)
        existing = next((a for a in registry.areas if a.name == name), None)
        if existing is not None:
            return registry, existing.area_id
        if len(registry.areas) >= MAX_AREAS:
            report.skipped.append({"ref": name, "reason": "AREA_LIMIT_EXCEEDED"})
            return registry, None
        area = Area(area_id=_area_id(name, registry), name=name, order=len(registry.areas))
        registry = await self._writer.create_area(owner_id, area, registry.revision)
        return registry, area.area_id


def _facts(device: Device) -> dict[str, Any]:
    return device.model_dump(mode="json", exclude={"synced_at_ms"})


def _clip_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip() or "设备"
    return cleaned[:NAME_MAX]


def _unique_name(
    registry: Registry, area_id: str | None, name: str, *, except_id: str | None = None
) -> str:
    taken = {d.name for d in registry.devices if d.area_id == area_id and d.device_id != except_id}
    if name not in taken:
        return name
    for index in range(2, 100):
        suffix = f"{index}"
        candidate = name[: NAME_MAX - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise ValueError("cannot find a unique device name")


_IDENT = re.compile(r"[^A-Za-z0-9._:-]+")


def _device_id(provider: str, external_ref: str) -> str:
    return (_IDENT.sub("-", f"{provider}.{external_ref}"))[:128]


def _area_id(name: str, registry: Registry) -> str:
    base = _IDENT.sub("-", name).strip("-") or "area"
    if not re.match(r"^[A-Za-z0-9]", base):
        base = "area-" + base
    candidate = base
    index = 2
    while any(a.area_id == candidate for a in registry.areas):
        candidate = f"{base}-{index}"
        index += 1
    return candidate[:128]
