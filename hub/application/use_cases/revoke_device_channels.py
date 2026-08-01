"""Revoke every channel lease owned by one disconnected device."""

from __future__ import annotations

from hub.application.use_cases.revoke_channel import RevokeChannel
from hub.ports.repositories import ChannelLeaseRepository


class RevokeDeviceChannels:
    def __init__(self, *, leases: ChannelLeaseRepository, revoke_channel: RevokeChannel) -> None:
        self._leases = leases
        self._revoke_channel = revoke_channel

    async def execute(self, device_id: str, *, reason: str) -> None:
        for lease in await self._leases.list_for_device(device_id):
            await self._revoke_channel.execute(lease.channel_id, reason=reason)
