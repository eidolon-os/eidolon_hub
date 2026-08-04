"""Bounded, structured queries over the public device directory."""

from __future__ import annotations

from dataclasses import dataclass

from hub.domain.devices.entities import DeviceDirectoryEntry, DeviceLifecycleState
from hub.ports.repositories import DeviceDirectoryRepository


@dataclass(frozen=True, slots=True)
class DeviceListQuery:
    owner_scope: str
    lifecycle_state: DeviceLifecycleState | None = None
    device_kind: str | None = None
    capability: str | None = None
    q: str | None = None
    after: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not self.owner_scope.strip() or len(self.owner_scope) > 64:
            raise ValueError("a bounded owner_scope is required")
        if not 1 <= self.limit <= 100:
            raise ValueError("device query limit must be between 1 and 100")
        bounds = {"device_kind": 96, "capability": 128, "q": 128, "after": 128}
        for name, maximum in bounds.items():
            value = getattr(self, name)
            if value is not None and (not value.strip() or len(value) > maximum):
                raise ValueError(f"{name} must be null or bounded")


@dataclass(frozen=True, slots=True)
class DeviceListPage:
    entries: tuple[DeviceDirectoryEntry, ...]
    next_cursor: str | None


class ListDevices:
    def __init__(self, directory: DeviceDirectoryRepository) -> None:
        self._directory = directory

    async def execute(self, query: DeviceListQuery) -> DeviceListPage:
        entries = await self._directory.list(owner_scope=query.owner_scope)
        q = query.q.strip().casefold() if query.q is not None else None
        device_kind = query.device_kind.strip() if query.device_kind is not None else None
        capability = query.capability.strip() if query.capability is not None else None
        after = query.after.strip() if query.after is not None else None
        filtered = tuple(
            entry
            for entry in sorted(entries, key=lambda value: value.device_id)
            if (after is None or entry.device_id > after)
            and (query.lifecycle_state is None or entry.lifecycle_state is query.lifecycle_state)
            and (device_kind is None or entry.device_kind == device_kind)
            and (capability is None or entry.manifest.declares_capability(capability))
            and (q is None or q in entry.device_id.casefold() or q in entry.display_name.casefold())
        )
        page = filtered[: query.limit]
        return DeviceListPage(
            entries=page,
            next_cursor=(page[-1].device_id if len(filtered) > query.limit else None),
        )
