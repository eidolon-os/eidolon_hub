"""Revoke exactly one canonical Claim generation."""

from __future__ import annotations

from dataclasses import replace

from hub.contracts.bindings.device import DeviceRevocationRequest, revoke_claim_fingerprint
from hub.domain.devices.entities import DeviceLifecycleState, DeviceRef
from hub.ports.claim_lifecycle import (
    ClaimCommandResult,
    ClaimEventRecord,
    ClaimLifecycleStore,
)
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryProjector, DeviceRepository


class RevokeDevice:
    """Admission Authority command handler.

    The handler owns only Claim state. It does not call Channel, Kernel or a
    delivery provider. A committed Claim event is the durable boundary those
    independently recoverable consumers observe.
    """

    def __init__(
        self,
        *,
        devices: DeviceRepository,
        claims: ClaimLifecycleStore,
        clock: Clock,
        ids: IdGenerator,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._claims = claims
        self._clock = clock
        self._ids = ids
        self._directory_projector = directory_projector

    async def execute(
        self,
        *,
        device_ref: DeviceRef,
        reason: str,
        command_id: str,
        correlation_id: str,
        principal_id: str,
    ) -> ClaimCommandResult:
        fingerprint = revoke_claim_fingerprint(
            DeviceRevocationRequest(
                command_id=command_id,
                correlation_id=correlation_id,
                device_ref=device_ref,
                reason=reason,
            )
        )
        replay = await self._claims.get_command(
            owner_domain_id=device_ref.owner_domain_id,
            command_type="device.claim.revoke",
            command_id=command_id,
        )
        if replay is not None:
            if replay.fingerprint != fingerprint:
                raise ValueError("command_id was reused with different content")
            return replay

        current = await self._devices.get(device_ref.device_instance_id)
        if current is None:
            raise KeyError(device_ref.device_instance_id)
        if current.owner_id != device_ref.owner_domain_id:
            raise PermissionError("device does not belong to that Owner Domain")
        if current.device_ref != device_ref:
            raise LookupError("Claim generation or trust epoch is stale")

        now = self._clock.now()
        if current.lifecycle_state is DeviceLifecycleState.REVOKED:
            return await self._claims.commit_terminal_result(
                device=current,
                command_id=command_id,
                fingerprint=fingerprint,
                occurred_at=current.updated_at,
            )

        revoked = replace(
            current,
            lifecycle_state=DeviceLifecycleState.REVOKED,
            updated_at=now,
            aggregate_revision=current.aggregate_revision + 1,
        )
        event = ClaimEventRecord(
            event_id=self._ids.new("claim-event"),
            event_type="live.eidolon.device.claim-revoked.v1",
            device_ref=device_ref,
            aggregate_revision=revoked.aggregate_revision,
            correlation_id=correlation_id,
            causation_id=command_id,
            actor_principal_id=principal_id,
            occurred_at=now,
            reason=reason,
        )
        result = await self._claims.commit_revoke(
            expected=current,
            revoked=revoked,
            command_id=command_id,
            fingerprint=fingerprint,
            event=event,
        )
        await self._directory_projector.execute(device_ref.device_instance_id)
        return result
