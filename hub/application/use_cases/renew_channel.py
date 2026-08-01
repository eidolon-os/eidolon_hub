"""Renew provider-owned channel credentials without retaining the old binding."""

from __future__ import annotations

from hub.domain.channels.entities import ChannelGrant
from hub.domain.channels.selection import ChannelProfileCatalog
from hub.ports.channels import ChannelGrantSender, ChannelProvisioner
from hub.ports.identity import Clock
from hub.ports.repositories import ChannelLeaseRepository, ConnectionRepository


class RenewChannel:
    def __init__(
        self,
        *,
        profiles: ChannelProfileCatalog,
        provisioners: dict[str, ChannelProvisioner],
        leases: ChannelLeaseRepository,
        connections: ConnectionRepository,
        grant_sender: ChannelGrantSender,
        clock: Clock,
    ) -> None:
        self._profiles = profiles
        self._provisioners = provisioners
        self._leases = leases
        self._connections = connections
        self._grant_sender = grant_sender
        self._clock = clock

    async def execute(self, channel_id: str) -> ChannelGrant:
        lease = await self._leases.get(channel_id)
        if lease is None:
            raise KeyError(channel_id)
        profile = self._profiles.get(lease.profile_name)
        provider = self._provisioners[profile.provisioner_ref]
        active = await self._connections.active_for_device(lease.device_id, now=self._clock.now())
        if not active:
            raise ConnectionError("device has no active connection for renewed grant")
        grant = await provider.renew(lease)
        if grant.lease.channel_id != channel_id or grant.lease.device_id != lease.device_id:
            await provider.revoke(grant.lease, reason="invalid-renew-response")
            raise RuntimeError("channel provider returned a mismatched renewal")
        try:
            await self._leases.upsert(grant.lease)
            await self._grant_sender.send_grant(signaling_ref=active[0].signaling_ref, grant=grant)
        except Exception:
            await provider.revoke(grant.lease, reason="renew-grant-delivery-failed")
            await self._leases.delete(channel_id)
            raise
        return grant
