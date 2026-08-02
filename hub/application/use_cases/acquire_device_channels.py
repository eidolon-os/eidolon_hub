"""Acquire Provider-owned channels for one authenticated device session."""

from __future__ import annotations

import hmac
from dataclasses import replace
from datetime import timedelta

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelKind,
    ChannelState,
    ProviderDeviceContext,
)
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock
from hub.ports.repositories import (
    ChannelLeaseRepository,
    DeviceRepository,
    DeviceSessionRepository,
)


class AcquireDeviceChannels:
    def __init__(
        self,
        *,
        hub_id: str,
        devices: DeviceRepository,
        sessions: DeviceSessionRepository,
        channels: ChannelLeaseRepository,
        provider: ChannelProviderControl,
        clock: Clock,
        minimum_lifetime: timedelta = timedelta(seconds=30),
    ) -> None:
        self._hub_id = hub_id
        self._devices = devices
        self._sessions = sessions
        self._channels = channels
        self._provider = provider
        self._clock = clock
        self._minimum_lifetime = minimum_lifetime

    async def execute(
        self,
        *,
        session_id: str,
        lease_token: str,
        request_id: str,
    ) -> ChannelAssignmentSet:
        if not request_id.strip() or len(request_id) > 96:
            raise ValueError("a bounded channel acquisition request_id is required")
        now = self._clock.now()
        session = await self._sessions.get(session_id)
        if (
            session is None
            or not hmac.compare_digest(session.lease_token, lease_token)
            or not session.is_active(now)
        ):
            raise PermissionError("active device session required")
        device = await self._devices.get(session.device_id)
        if (
            device is None
            or not device.approved
            or device.revoked
            or device.identity.public_key_fingerprint != session.identity_fingerprint
        ):
            raise PermissionError("registered and approved device required")

        context = ProviderDeviceContext(
            operation_id=request_id,
            hub_id=self._hub_id,
            device_id=device.identity.device_id,
            public_key_fingerprint=device.identity.public_key_fingerprint,
            tenant_id=device.identity.tenant_id,
            owner_id=device.owner_id,
            display_name=device.display_name,
            device_kind=device.device_kind,
            manifest_json=device.manifest_json,
            manifest_revision=device.manifest_revision,
            approved=device.approved,
            revoked=device.revoked,
            connected=True,
        )
        assignments = await self._provider.acquire_channels(context)
        self._validate(assignments, context=context)
        await self._persist_metadata(assignments)
        return assignments

    def _validate(
        self,
        assignments: ChannelAssignmentSet,
        *,
        context: ProviderDeviceContext,
    ) -> None:
        if (
            assignments.operation_id != context.operation_id
            or assignments.device_id != context.device_id
            or assignments.manifest_revision != context.manifest_revision
        ):
            raise ValueError("Provider assignments do not match channel acquisition")
        if not any(
            grant.lease.purpose == "management" and ChannelKind.RELIABLE_DATA in grant.lease.kinds
            for grant in assignments.grants
        ):
            raise ValueError("Provider did not assign a reliable management channel")
        minimum_expiry = self._clock.now() + self._minimum_lifetime
        for grant in assignments.grants:
            if (
                grant.operation_id != context.operation_id
                or grant.lease.device_id != context.device_id
                or grant.lease.state is not ChannelState.PENDING
                or grant.lease.expires_at <= minimum_expiry
            ):
                raise ValueError("Provider returned an invalid channel assignment")

    async def _persist_metadata(self, assignments: ChannelAssignmentSet) -> None:
        now = self._clock.now()
        previous = {
            lease.channel_id: lease
            for lease in await self._channels.list_for_device(assignments.device_id)
        }
        assigned_ids = {grant.lease.channel_id for grant in assignments.grants}
        for channel_id, lease in previous.items():
            if channel_id not in assigned_ids and lease.state in {
                ChannelState.PENDING,
                ChannelState.ACTIVE,
            }:
                await self._channels.upsert(lease.transition(ChannelState.CLOSED, occurred_at=now))
        for grant in assignments.grants:
            lease = grant.lease
            prior = previous.get(lease.channel_id)
            if prior is not None and prior.state is ChannelState.ACTIVE:
                lease = replace(
                    lease,
                    state=ChannelState.ACTIVE,
                    updated_at=prior.updated_at,
                )
            await self._channels.upsert(lease)
