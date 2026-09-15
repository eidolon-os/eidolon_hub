"""Owner-scoped output policy, using the existing device/audit transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from hub.admission.domain import ActorContext
from hub.contracts.bindings.device import DeviceRef
from hub.contracts.bindings.presentation import DeviceOutputPolicy
from hub.contracts.bindings.presentation import SetDeviceOutputPolicy as SetOutputPolicy
from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.identity import Clock, IdGenerator
from hub.ports.management_events import DeviceManagementEventRecord
from hub.ports.repositories import DeviceMutationUnitOfWork, DeviceRepository

POLICY_WRITE_SCOPE = "device.output-policy.write"


class OutputPolicyConflict(ValueError):
    pass


class UpdateDeviceOutputPolicy:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        mutations: DeviceMutationUnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        on_changed: Callable[[DeviceRef], Awaitable[object]] | None = None,
    ):
        self._devices, self._mutations, self._clock, self._ids = devices, mutations, clock, ids
        self._on_changed = on_changed

    async def execute(
        self, *, command: SetOutputPolicy, context: ActorContext
    ) -> DeviceOutputPolicy:
        context.require_scope(POLICY_WRITE_SCOPE)
        current = await self._devices.get(command.device_ref.device_instance_id)
        if current is None or current.lifecycle_state != DeviceLifecycleState.APPROVED:
            raise KeyError(command.device_ref.device_instance_id)
        if (
            str(context.owner_domain_id) != current.owner_domain_id
            or str(context.business_owner_id) != current.owner_id
        ):
            raise PermissionError("output policy belongs to another Owner")
        if current.device_ref != command.device_ref:
            raise OutputPolicyConflict("device generation changed")
        revision = current.output_policy.revision if current.output_policy else 0
        if revision != command.expected_revision:
            # A retry of an already applied update returns its result. An older
            # revision can never widen or roll back the current policy.
            if (
                current.output_policy is not None
                and revision == command.expected_revision + 1
                and current.output_policy.allowed == command.allowed
            ):
                if self._on_changed is not None:
                    await self._on_changed(command.device_ref)
                return current.output_policy
            raise OutputPolicyConflict("output policy revision changed")
        policy = DeviceOutputPolicy(revision=revision + 1, allowed=command.allowed)
        now = self._clock.now()
        await self._mutations.commit(
            expected=current,
            device=replace(
                current,
                output_policy=policy,
                updated_at=now,
                aggregate_revision=current.aggregate_revision + 1,
            ),
            event=DeviceManagementEventRecord(
                event_id=self._ids.new("output-policy"),
                event_type="device.output-policy.changed",
                source="urn:eidolon:authority:device-control",
                principal_id=context.actor.principal_id,
                subject=current.identity.device_id,
                occurred_at=now,
                data={
                    "owner_id": current.owner_id,
                    "device_ref": command.device_ref.model_dump(mode="json"),
                    "output_policy": policy.model_dump(mode="json"),
                },
            ),
        )
        if self._on_changed is not None:
            await self._on_changed(command.device_ref)
        return policy
