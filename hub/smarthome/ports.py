"""Device execution and integration ports; the registry remains owned by System Data."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eidolon_sdk.biz.smarthome import (
    AccountSchema,
    Command,
    Device,
    DiscoveredDevice,
    Observation,
    ProviderAccount,
    Registry,
)


class RegistrySource(Protocol):
    async def get(self, owner_id: str) -> Registry:
        """The Owner's current registry. Raises if System Data cannot answer."""
        ...


@dataclass(frozen=True, slots=True)
class Delegated:
    """A platform took the instruction in its own words; no device state was observed.

    The runtime records it as ``delegated``. It never becomes ``succeeded``.
    """

    answer: str


class SmartHomeProvider(Protocol):
    """Owns the state of the devices it implements, per Owner.

    Three confirmation styles, decided by the adapter and visible to the
    runtime only through what ``execute`` returns:

    - self-confirming: the returned state is final (virtual devices, Matter);
    - event-confirming: the adapter returns only after the ecosystem reported
      the new state, and raises ``TimeoutError`` otherwise (Home Assistant);
    - delegated: the adapter returns ``Delegated`` and the runtime never claims
      the device changed.

    ``pushes_observations`` tells the runtime whether the adapter feeds the
    observation cache itself (through ``ProviderIntegration.observe``); when it
    is False the runtime polls ``states``.
    """

    pushes_observations: bool

    async def reconcile(self, owner_id: str, devices: Sequence[Device]) -> None:
        """Converge on exactly these devices: start new ones, forget removed ones."""
        ...

    async def states(self, owner_id: str, devices: Sequence[Device]) -> dict[str, dict[str, Any]]:
        """Each reachable device's full current state, keyed by device_id."""
        ...

    async def execute(
        self, owner_id: str, device: Device, command: Command
    ) -> dict[str, Any] | Delegated:
        """Carry out one command and return the device's full state after it, or ``Delegated``.

        Raises ``SmartHomeError`` for a command it refused without acting.
        """
        ...


class BindError(Exception):
    """An account could not be bound; ``code`` is stable, ``message`` is for the person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ProviderIntegration(Protocol):
    """What an account-backed Provider adds on top of execution."""

    kind: str

    def account_schema(self) -> AccountSchema: ...

    async def bind(
        self, owner_id: str, account_id: str, fields: Mapping[str, str]
    ) -> ProviderAccount:
        """Verify the fields, store what must be kept, connect. Raises ``BindError``."""
        ...

    async def unbind(self, owner_id: str, account_id: str) -> None: ...

    async def discover(self, owner_id: str, account_id: str) -> list[DiscoveredDevice]: ...

    def observe(self, owner_id: str, account_id: str) -> AsyncIterator[Observation]:
        """Observations as they arrive; a polling adapter yields on its own schedule."""
        ...

    async def restore(self) -> None:
        """Reconnect every account bound before this process started."""
        ...
