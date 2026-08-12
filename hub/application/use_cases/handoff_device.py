"""Relay a human-approved enrollment to the external Channel Provider."""

from __future__ import annotations

from dataclasses import dataclass

from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.domain.channels.entities import ChannelAssignmentSet
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock, RetrievalTokenHasher
from hub.ports.repositories import DeviceRepository


@dataclass(frozen=True, slots=True)
class DeviceHandoffOutcome:
    device: ManagedDevice
    assignments: ChannelAssignmentSet | None


class HandoffDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        provision: ProvisionDeviceChannels,
        tokens: RetrievalTokenHasher,
        clock: Clock,
    ) -> None:
        self._devices = devices
        self._provision = provision
        self._tokens = tokens
        self._clock = clock

    async def execute(
        self, *, enrollment_id: str, retrieval_token: str
    ) -> DeviceHandoffOutcome:
        device = await self._devices.get_by_enrollment_id(enrollment_id)
        if device is None:
            raise KeyError(enrollment_id)
        if not self._tokens.verify(retrieval_token, device.retrieval_token_hash):
            raise PermissionError("invalid enrollment retrieval token")
        if device.lifecycle_state is DeviceLifecycleState.REVOKED:
            return DeviceHandoffOutcome(device=device, assignments=None)
        if device.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL:
            # The window bounds how long an enrollment nobody has approved may
            # sit waiting to be collected. Past it, the device must enroll again.
            if device.retrieval_expires_at <= self._clock.now():
                raise TimeoutError("enrollment retrieval window expired")
            return DeviceHandoffOutcome(device=device, assignments=None)
        # An approved device asks for channel credentials every time it opens a
        # conversation, for as long as it is in service. What ends that service
        # is the owner revoking it — not the clock that was started to bound a
        # pickup nobody had performed yet.
        assignments = await self._provision.execute(
            device=device,
            operation_id=device.enrollment_id,
        )
        return DeviceHandoffOutcome(device=device, assignments=assignments)
