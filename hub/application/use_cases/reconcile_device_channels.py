"""Converge persisted device facts into Provider-owned channel assignments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelState,
    ProviderDeviceContext,
    ProviderSyncRecord,
    ProviderSyncState,
)
from hub.domain.devices.entities import ManagedDevice
from hub.ports.channels import ChannelGrantSender, ChannelProviderControl
from hub.ports.identity import Clock
from hub.ports.repositories import (
    ChannelLeaseRepository,
    ChannelProviderSyncRepository,
    ConnectionRepository,
    DeviceRepository,
)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    scanned: int = 0
    skipped: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0


class ReconcileDeviceChannels:
    def __init__(
        self,
        *,
        hub_id: str,
        hub_instance_id: str,
        devices: DeviceRepository,
        connections: ConnectionRepository,
        syncs: ChannelProviderSyncRepository,
        channel_leases: ChannelLeaseRepository,
        provider: ChannelProviderControl,
        grant_sender: ChannelGrantSender,
        clock: Clock,
        claim_ttl: timedelta = timedelta(seconds=30),
        grant_redelivery_after: timedelta = timedelta(seconds=30),
        refresh_before: timedelta = timedelta(seconds=30),
    ) -> None:
        self._hub_id = hub_id
        self._hub_instance_id = hub_instance_id
        self._devices = devices
        self._connections = connections
        self._syncs = syncs
        self._leases = channel_leases
        self._provider = provider
        self._grant_sender = grant_sender
        self._clock = clock
        self._claim_ttl = claim_ttl
        self._grant_redelivery_after = grant_redelivery_after
        self._refresh_before = refresh_before

    async def execute(self) -> ReconcileResult:
        scanned = skipped = claimed = succeeded = failed = 0
        for device in await self._devices.list_all():
            scanned += 1
            now = self._clock.now()
            active_connections = await self._connections.active_for_device(
                device.identity.device_id, now=now
            )
            prior = await self._syncs.get(device.identity.device_id)
            if not device.approved and prior is None:
                skipped += 1
                continue
            context, desired = self._desired(device, connected=bool(active_connections))
            if prior is not None and prior.desired_revision == desired.desired_revision:
                context = replace(context, operation_id=prior.operation_id)
                desired = replace(desired, operation_id=prior.operation_id)
                if prior.state is ProviderSyncState.FAILED:
                    backoff_seconds = min(60, 2 ** min(prior.attempts, 6))
                    if now < prior.updated_at + timedelta(seconds=backoff_seconds):
                        skipped += 1
                        continue
                if prior.state is ProviderSyncState.SUCCEEDED:
                    leases = await self._leases.list_for_device(device.identity.device_id)
                    if self._is_converged(
                        device=device,
                        connected=bool(active_connections),
                        leases=leases,
                        now=now,
                    ):
                        skipped += 1
                        continue
                    rotated = self._rotated_operation_id(
                        desired_revision=desired.desired_revision,
                        prior_operation_id=prior.operation_id,
                        leases=leases,
                        now=now,
                    )
                    context = replace(context, operation_id=rotated)
                    desired = replace(desired, operation_id=rotated)
            claim = await self._syncs.try_claim(
                desired,
                owner_instance_id=self._hub_instance_id,
                now=now,
                claim_ttl=self._claim_ttl,
            )
            if claim is None:
                skipped += 1
                continue
            claimed += 1
            try:
                assignments = await self._provider.sync_device(context)
                self._validate(assignments, context=context, now=self._clock.now())
                await self._apply(
                    assignments,
                    signaling_ref=(
                        active_connections[0].signaling_ref if active_connections else None
                    ),
                )
                await self._syncs.mark_succeeded(
                    device_id=device.identity.device_id,
                    operation_id=context.operation_id,
                    now=self._clock.now(),
                )
                succeeded += 1
            except Exception as exc:
                await self._syncs.mark_failed(
                    device_id=device.identity.device_id,
                    operation_id=context.operation_id,
                    now=self._clock.now(),
                    error=type(exc).__name__,
                )
                failed += 1
        return ReconcileResult(scanned, skipped, claimed, succeeded, failed)

    def _desired(
        self, device: ManagedDevice, *, connected: bool
    ) -> tuple[ProviderDeviceContext, ProviderSyncRecord]:
        now = self._clock.now()
        desired_json = json.dumps(
            {
                "approved": device.approved,
                "connected": connected,
                "device_id": device.identity.device_id,
                "device_kind": device.device_kind,
                "display_name": device.display_name,
                "hub_id": self._hub_id,
                "manifest_revision": device.manifest_revision,
                "owner_id": device.owner_id,
                "public_key_fingerprint": device.identity.public_key_fingerprint,
                "revoked": device.revoked,
                "tenant_id": device.identity.tenant_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        desired_revision = "sha256:" + hashlib.sha256(desired_json.encode()).hexdigest()
        operation_id = f"channel-sync:{desired_revision}"
        context = ProviderDeviceContext(
            operation_id=operation_id,
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
            connected=connected,
        )
        desired = ProviderSyncRecord(
            device_id=device.identity.device_id,
            operation_id=operation_id,
            desired_revision=desired_revision,
            state=ProviderSyncState.PENDING,
            attempts=0,
            updated_at=now,
        )
        return context, desired

    def _validate(
        self,
        assignments: ChannelAssignmentSet,
        *,
        context: ProviderDeviceContext,
        now: datetime,
    ) -> None:
        if (
            assignments.operation_id != context.operation_id
            or assignments.device_id != context.device_id
            or assignments.manifest_revision != context.manifest_revision
        ):
            raise ValueError("Provider assignments do not match desired device state")
        should_be_closed = context.revoked or not context.approved or not context.connected
        if should_be_closed and assignments.grants:
            raise ValueError("Provider returned channels for an unavailable device")
        if not should_be_closed and not any(
            grant.lease.purpose == "management" and ChannelKind.RELIABLE_DATA in grant.lease.kinds
            for grant in assignments.grants
        ):
            raise ValueError("Provider did not assign a reliable management data channel")
        for grant in assignments.grants:
            if (
                grant.operation_id != context.operation_id
                or grant.lease.device_id != context.device_id
                or grant.lease.state is not ChannelState.PENDING
            ):
                raise ValueError("Provider returned a mismatched channel grant")
            if grant.lease.expires_at <= now + self._refresh_before:
                raise ValueError("Provider returned a channel grant with insufficient lifetime")

    async def _apply(self, assignments: ChannelAssignmentSet, *, signaling_ref: str | None) -> None:
        previous = {
            lease.channel_id: lease
            for lease in await self._leases.list_for_device(assignments.device_id)
        }
        delivered: list[ChannelGrant] = []
        try:
            for grant in assignments.grants:
                if signaling_ref is None:
                    raise ValueError("cannot deliver a channel grant without an active connection")
                delivered_grant = replace(
                    grant,
                    lease=replace(grant.lease, updated_at=self._clock.now()),
                )
                await self._leases.upsert(delivered_grant.lease)
                delivered.append(delivered_grant)
                await self._grant_sender.send_grant(
                    signaling_ref=signaling_ref,
                    grant=delivered_grant,
                )
        except Exception:
            for grant in delivered:
                prior = previous.get(grant.lease.channel_id)
                if prior is None:
                    await self._leases.delete(grant.lease.channel_id)
                else:
                    await self._leases.upsert(prior)
            raise
        current_ids = {grant.lease.channel_id for grant in assignments.grants}
        for channel_id in set(previous) - current_ids:
            await self._leases.delete(channel_id)

    def _is_converged(
        self,
        *,
        device: ManagedDevice,
        connected: bool,
        leases: tuple[ChannelLease, ...],
        now: datetime,
    ) -> bool:
        unavailable = device.revoked or not device.approved or not connected
        if unavailable:
            return not leases
        if any(
            lease.is_active(now) and lease.expires_at > now + self._refresh_before
            for lease in leases
        ):
            return True
        return any(
            lease.state is ChannelState.PENDING
            and lease.updated_at is not None
            and lease.updated_at + self._grant_redelivery_after > now
            for lease in leases
        )

    def _rotated_operation_id(
        self,
        *,
        desired_revision: str,
        prior_operation_id: str,
        leases: tuple[ChannelLease, ...],
        now: datetime,
    ) -> str:
        """Rotate only terminal/expiring assignments; stale pending grants replay safely."""

        pending_are_still_usable = leases and all(
            lease.state is ChannelState.PENDING and lease.expires_at > now + self._refresh_before
            for lease in leases
        )
        if pending_are_still_usable or not leases:
            return prior_operation_id
        lease_generation = [
            {
                "channel_id": lease.channel_id,
                "expires_at": lease.expires_at.isoformat(),
                "state": lease.state.value,
                "updated_at": lease.updated_at.isoformat() if lease.updated_at else "",
            }
            for lease in sorted(leases, key=lambda item: item.channel_id)
        ]
        generation_json = json.dumps(
            {"desired_revision": desired_revision, "leases": lease_generation},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "channel-sync:sha256:" + hashlib.sha256(generation_json.encode()).hexdigest()
