"""External channel provider and grant-delivery ports."""

from __future__ import annotations

from typing import Protocol

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelLifecycle,
    ProviderDeviceContext,
)
from hub.domain.commands.entities import DeviceCommand


class ChannelProviderControl(Protocol):
    async def acquire_channels(self, context: ProviderDeviceContext) -> ChannelAssignmentSet: ...


class CommandChannelSender(Protocol):
    async def send_command(self, command: DeviceCommand) -> None: ...


class DataEnvelopeIngress(Protocol):
    async def ingest_raw(self, raw: bytes) -> bool: ...


class ChannelLifecycleIngress(Protocol):
    async def execute(self, lifecycle: ChannelLifecycle) -> None: ...
