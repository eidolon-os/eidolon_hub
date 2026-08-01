"""External channel provider and grant-delivery ports."""

from __future__ import annotations

from typing import Protocol

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelLifecycle,
    ProviderDeviceContext,
)
from hub.domain.commands.entities import DeviceCommand


class ChannelProviderControl(Protocol):
    async def sync_device(self, context: ProviderDeviceContext) -> ChannelAssignmentSet: ...


class ChannelGrantSender(Protocol):
    async def send_grant(self, *, signaling_ref: str, grant: ChannelGrant) -> None: ...


class CommandChannelSender(Protocol):
    async def send_command(self, command: DeviceCommand) -> None: ...


class DataEnvelopeIngress(Protocol):
    async def ingest_raw(self, raw: bytes) -> bool: ...


class ChannelLifecycleIngress(Protocol):
    async def execute(self, lifecycle: ChannelLifecycle) -> None: ...
