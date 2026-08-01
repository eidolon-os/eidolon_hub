"""Provision a channel and relay an opaque grant over the preferred connection."""

from __future__ import annotations

from datetime import timedelta

from hub.domain.channels.entities import ChannelGrant, ChannelRequest
from hub.domain.channels.selection import ChannelProfileCatalog
from hub.ports.channels import ChannelGrantSender, ChannelProvisioner
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import (
    ChannelLeaseRepository,
    ConnectionRepository,
    DeviceRepository,
)


class DeviceUnavailable(RuntimeError):
    pass


class ProvisionChannel:
    def __init__(
        self,
        *,
        profiles: ChannelProfileCatalog,
        provisioners: dict[str, ChannelProvisioner],
        grant_sender: ChannelGrantSender,
        devices: DeviceRepository,
        connections: ConnectionRepository,
        channel_leases: ChannelLeaseRepository,
        clock: Clock,
        ids: IdGenerator,
        request_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._profiles = profiles
        self._provisioners = dict(provisioners)
        self._grant_sender = grant_sender
        self._devices = devices
        self._connections = connections
        self._channel_leases = channel_leases
        self._clock = clock
        self._ids = ids
        self._request_ttl = request_ttl

    async def execute(self, *, device_id: str, profile_name: str) -> ChannelGrant:
        device = await self._devices.get(device_id)
        if device is None or device.revoked or not device.approved:
            raise DeviceUnavailable(device_id)
        now = self._clock.now()
        active = await self._connections.active_for_device(device_id, now=now)
        if not active:
            raise DeviceUnavailable(device_id)
        profile = self._profiles.get(profile_name)
        try:
            provisioner = self._provisioners[profile.provisioner_ref]
        except KeyError as exc:
            raise RuntimeError(
                f"channel provisioner is not configured: {profile.provisioner_ref}"
            ) from exc
        request = ChannelRequest(
            request_id=self._ids.new("channel-request"),
            device_id=device_id,
            profile=profile,
            requested_at=now,
            expires_at=now + self._request_ttl,
        )
        grant = await provisioner.provision(request)
        if grant.request_id != request.request_id or grant.lease.device_id != device_id:
            await provisioner.revoke(grant.lease, reason="invalid-provider-response")
            raise RuntimeError("channel provisioner returned a mismatched grant")
        # The binding is neither decoded nor persisted. It is immediately
        # handed to the selected connection adapter for signaling.
        await self._channel_leases.upsert(grant.lease)
        try:
            await self._grant_sender.send_grant(signaling_ref=active[0].signaling_ref, grant=grant)
        except Exception:
            await provisioner.revoke(grant.lease, reason="grant-delivery-failed")
            await self._channel_leases.delete(grant.lease.channel_id)
            raise
        return grant
