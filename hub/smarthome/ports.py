"""Device execution ports; registry remains owned by System Data."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from eidolon_sdk.biz.smarthome import Command, Device, Registry


class RegistrySource(Protocol):
    async def get(self, owner_id: str) -> Registry:
        """The Owner's current registry. Raises if System Data cannot answer."""
        ...


class SmartHomeProvider(Protocol):
    """Owns the state of the devices it implements, per Owner."""

    async def reconcile(self, owner_id: str, devices: Sequence[Device]) -> None:
        """Converge on exactly these devices: start new ones, forget removed ones."""
        ...

    async def states(self, owner_id: str, devices: Sequence[Device]) -> dict[str, dict[str, Any]]:
        """Each reachable device's full current state, keyed by device_id."""
        ...

    async def execute(self, owner_id: str, device: Device, command: Command) -> dict[str, Any]:
        """Carry out one command and return the device's full state after it.

        Raises ``SmartHomeError`` for a command it refused without acting.
        """
        ...
