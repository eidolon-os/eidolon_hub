"""Ports for Hub-owned, durable device management audit events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | dict[str, "JsonValue"]
)
ManagementEventData: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class DeviceManagementEventRecord:
    event_id: str
    event_type: str
    source: str
    subject: str
    occurred_at: datetime
    data: ManagementEventData

    def __post_init__(self) -> None:
        identifiers = (self.event_id, self.event_type, self.source, self.subject)
        if any(not value.strip() for value in identifiers):
            raise ValueError("management event identifiers are required")
        if self.occurred_at.tzinfo is None:
            raise ValueError("management event timestamp must be timezone-aware")
        if not isinstance(self.data, dict):
            raise ValueError("management event data must be an object")


@dataclass(frozen=True, slots=True)
class StoredDeviceManagementEvent:
    stream_position: int
    event: DeviceManagementEventRecord

    def __post_init__(self) -> None:
        if self.stream_position < 1:
            raise ValueError("management event stream_position must be positive")


class DeviceManagementEventSink(Protocol):
    async def publish(self, event: DeviceManagementEventRecord) -> None: ...


class DeviceManagementEventStream(Protocol):
    async def list_after(
        self, *, owner_scope: str, stream_position: int, limit: int
    ) -> tuple[StoredDeviceManagementEvent, ...]: ...
