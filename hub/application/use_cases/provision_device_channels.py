"""Provision Provider-owned channels for an approved device handoff."""

from __future__ import annotations

from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ProviderDeviceContext,
)
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.channels import ChannelProviderContractError, ChannelProviderControl
from hub.ports.identity import Clock


class ProvisionDeviceChannels:
    """Pass enrollment context to a Provider without applying channel policy."""

    def __init__(
        self,
        *,
        hub_id: str,
        provider: ChannelProviderControl,
        clock: Clock,
    ) -> None:
        self._hub_id = hub_id
        self._provider = provider
        self._clock = clock

    async def execute(
        self,
        *,
        device: ManagedDevice,
        operation_id: str,
    ) -> ChannelAssignmentSet:
        if not operation_id.strip() or len(operation_id) > 96:
            raise ValueError("a bounded channel provisioning operation_id is required")
        if device.lifecycle_state is not DeviceLifecycleState.APPROVED:
            raise PermissionError("approved device required")

        context = ProviderDeviceContext(
            operation_id=operation_id,
            hub_id=self._hub_id,
            device_id=device.identity.device_id,
            owner_id=device.owner_id,
            display_name=device.display_name,
            device_kind=device.device_kind,
            manifest_json=device.manifest_json,
            manifest_revision=device.manifest_revision,
        )
        try:
            assignments = await self._provider.provision_channels(context)
        except ValueError as exc:
            raise ChannelProviderContractError(
                "Provider returned an invalid provision response"
            ) from exc
        self._validate(assignments, context=context)
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
            raise ChannelProviderContractError(
                "Provider assignments do not match channel provisioning"
            )
        now = self._clock.now()
        for grant in assignments.grants:
            if (
                grant.device_id != context.device_id
                or grant.expires_at <= now
            ):
                raise ChannelProviderContractError(
                    "Provider returned an invalid channel assignment"
                )
