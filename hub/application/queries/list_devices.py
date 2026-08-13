"""Bounded, structured queries over the public device directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hub.domain.devices.entities import DeviceDirectoryEntry, DeviceLifecycleState
from hub.ports.identity import Clock
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
    def __init__(self, directory: DeviceDirectoryRepository, *, clock: Clock) -> None:
        self._directory = directory
        self._clock = clock

    async def execute(self, query: DeviceListQuery) -> DeviceListPage:
        """List the devices this owner scope has, as they stand right now.

        Asking for the ones awaiting approval means asking which ones can be
        approved. An enrollment whose window has closed cannot: the Hub refuses
        it, and the device replaces it by enrolling again. So it is left out
        rather than offered and then refused — a claim reading this list has no
        other way to tell the difference, and matching a device by its id alone
        would have it seize a stale enrollment while the live one is seconds
        away.
        """

        now = self._clock.now()
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
            and (
                query.lifecycle_state is not DeviceLifecycleState.PENDING_APPROVAL
                or entry.awaits_approval(now=now)
            )
            and (device_kind is None or entry.device_kind == device_kind)
            and (capability is None or entry.manifest.declares_capability(capability))
            and (q is None or q in entry.device_id.casefold() or q in entry.display_name.casefold())
        )
        page = filtered[: query.limit]
        return DeviceListPage(
            entries=page,
            next_cursor=(page[-1].device_id if len(filtered) > query.limit else None),
        )
