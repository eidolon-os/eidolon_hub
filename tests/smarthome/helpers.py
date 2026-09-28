from __future__ import annotations

from typing import Any

from eidolon_sdk.biz.smarthome import (
    Command,
    ExecuteRequest,
    Origin,
    Registry,
)
from eidolon_sdk.biz.smarthome.samples import apartment

OWNER = "owner-a"
OTHER_OWNER = "owner-b"
PANEL = "panel-living"
NOW_MS = 1_700_000_000_000


def home(
    *, revision: int = 1, placements: dict[str, str] | None = None, **changes: Any
) -> Registry:
    """The sample apartment, with the given panels placed and fields replaced."""
    value = apartment().model_dump(mode="json")
    value["revision"] = revision
    placed = {PANEL: "living"} if placements is None else placements
    value["placements"] = [{"device_ref": ref, "area_id": area} for ref, area in placed.items()]
    value.update(changes)
    return Registry.model_validate(value)


def cmd(device_id: str, trait: str, command: str, **params: Any) -> Command:
    return Command(device_id=device_id, trait=trait, command=command, params=params)


def request(
    request_id: str,
    *commands: Command,
    kind: str = "voice",
    label: str | None = None,
    deadline_ms: int = NOW_MS + 3_000,
) -> ExecuteRequest:
    return ExecuteRequest(
        request_id=request_id,
        commands=commands,
        origin=Origin(kind=kind, label=label),
        deadline_ms=deadline_ms,
    )


def scene(
    request_id: str,
    scene_id: str,
    *,
    kind: str = "voice",
    label: str | None = None,
    deadline_ms: int = NOW_MS + 3_000,
) -> ExecuteRequest:
    return ExecuteRequest(
        request_id=request_id,
        scene_id=scene_id,
        origin=Origin(kind=kind, label=label),
        deadline_ms=deadline_ms,
    )


class Clock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class FakeRegistry:
    """What System Data would answer, per Owner."""

    def __init__(self, registries: dict[str, Registry]) -> None:
        self.registries = dict(registries)
        self.reads = 0

    async def get(self, owner_id: str) -> Registry:
        self.reads += 1
        return self.registries[owner_id]
