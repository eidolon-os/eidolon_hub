"""External channel provider and grant-delivery ports."""

from __future__ import annotations

from typing import Protocol

from hub.domain.channels.entities import ChannelGrant, ChannelLease, ChannelRequest
from hub.domain.commands.entities import DeviceCommand


class ChannelProvisioner(Protocol):
    async def provision(self, request: ChannelRequest) -> ChannelGrant: ...
    async def renew(self, lease: ChannelLease) -> ChannelGrant: ...
    async def revoke(self, lease: ChannelLease, *, reason: str) -> None: ...


class ChannelGrantSender(Protocol):
    async def send_grant(self, *, signaling_ref: str, grant: ChannelGrant) -> None: ...


class CommandChannelSender(Protocol):
    async def send_command(self, command: DeviceCommand) -> None: ...


class DataEnvelopeIngress(Protocol):
    async def ingest_raw(self, raw: bytes) -> bool: ...


class DeviceChannelRevoker(Protocol):
    async def execute(self, device_id: str, *, reason: str) -> None: ...
