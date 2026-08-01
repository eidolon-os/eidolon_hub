"""Revoke a channel through its configured external Provider."""

from __future__ import annotations

from hub.domain.channels.selection import ChannelProfileCatalog
from hub.ports.channels import ChannelProvisioner
from hub.ports.repositories import ChannelLeaseRepository


class RevokeChannel:
    def __init__(
        self,
        *,
        profiles: ChannelProfileCatalog,
        provisioners: dict[str, ChannelProvisioner],
        leases: ChannelLeaseRepository,
    ) -> None:
        self._profiles = profiles
        self._provisioners = provisioners
        self._leases = leases

    async def execute(self, channel_id: str, *, reason: str) -> None:
        lease = await self._leases.get(channel_id)
        if lease is None:
            return
        profile = self._profiles.get(lease.profile_name)
        await self._provisioners[profile.provisioner_ref].revoke(lease, reason=reason)
        await self._leases.delete(channel_id)
