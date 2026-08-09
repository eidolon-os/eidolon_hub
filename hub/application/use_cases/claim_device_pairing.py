"""Approve a pending enrollment using an authenticated local pairing proof."""

from __future__ import annotations

import hashlib
import hmac

from hub.application.use_cases.approve_device import ApproveDevice
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock
from hub.ports.repositories import DeviceRepository


class ClaimDevicePairing:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        approve: ApproveDevice,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._approve = approve
        self._clock = clock

    async def execute(
        self,
        *,
        enrollment_id: str,
        pairing_secret: str,
        owner_id: str,
        request_id: str,
        principal_id: str,
    ) -> ManagedDevice:
        if not owner_id.strip():
            raise PermissionError("pairing credential requires an Owner scope")
        current = await self._devices.get_by_enrollment_id(enrollment_id)
        if current is None:
            raise KeyError(enrollment_id)
        if not current.pairing_method or not current.pairing_secret_hash:
            raise PermissionError("enrollment does not support Owner pairing")
        candidate = "sha256:" + hashlib.sha256(pairing_secret.encode()).hexdigest()
        if not hmac.compare_digest(candidate, current.pairing_secret_hash):
            raise PermissionError("invalid local pairing proof")
        if (
            current.lifecycle_state is DeviceLifecycleState.APPROVED
            and current.owner_id != owner_id
        ):
            raise PermissionError("pairing proof is already bound to another Owner")
        if (
            current.lifecycle_state is DeviceLifecycleState.APPROVED
            and current.last_management_request_id != request_id
        ):
            raise PermissionError("local pairing proof was already consumed")
        if (
            current.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
            and current.retrieval_expires_at <= self._clock.now()
        ):
            raise ValueError("device enrollment expired")
        return await self._approve.execute(
            device_id=current.identity.device_id,
            owner_id=owner_id,
            request_id=request_id,
            principal_id=principal_id,
            approval_method=current.pairing_method,
        )
