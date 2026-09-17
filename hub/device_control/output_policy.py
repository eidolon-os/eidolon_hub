"""Owner-scoped output policy, using the existing device/audit transaction."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from hub.admission.domain import ActorContext
from hub.contracts.bindings.device import DeviceRef
from hub.contracts.bindings.presentation import (
    DeviceOutputConfiguration,
    DeviceOutputPolicy,
    manifest_outputs,
)
from hub.contracts.bindings.presentation import ReadDeviceOutputPolicy as ReadOutputPolicy
from hub.contracts.bindings.presentation import SetDeviceOutputPolicy as SetOutputPolicy
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.ports.identity import Clock, IdGenerator
from hub.ports.management_events import DeviceManagementEventRecord
from hub.ports.repositories import DeviceMutationUnitOfWork, DeviceRepository

POLICY_WRITE_SCOPE = "device.output-policy.write"
#: Reading what a device declared and what its Owner allowed is a device fact,
#: so it is the device read scope. A second scope for the read half of one
#: resource would be a second authorization to keep in step with this one.
POLICY_READ_SCOPE = "device.read"


class OutputPolicyConflict(ValueError):
    pass


async def _owned_device(
    devices: DeviceRepository, device_ref: DeviceRef, context: ActorContext
) -> ManagedDevice:
    """The one device this command may be about, or the reason it is not.

    Write and read answer the same three questions in the same order — does it
    exist and is it still admitted, is it this Owner's, and is it the
    generation the caller named — because a read that is laxer than the write
    is a way to learn about a device one may not change.
    """

    current = await devices.get(device_ref.device_instance_id)
    if current is None or current.lifecycle_state != DeviceLifecycleState.APPROVED:
        raise KeyError(device_ref.device_instance_id)
    if (
        str(context.owner_domain_id) != current.owner_domain_id
        or str(context.business_owner_id) != current.owner_id
    ):
        raise PermissionError("output policy belongs to another Owner")
    if current.device_ref != device_ref:
        raise OutputPolicyConflict("device generation changed")
    return current


class ReadDeviceOutputConfiguration:
    """Answer what this device can present and what its Owner has allowed.

    The capability half is derived from the accepted Manifest on every read
    rather than stored beside the policy: a device changes what it can do by
    asserting a new Manifest, and a copy kept here would be one more fact able
    to disagree with the one the Provider negotiates against.
    """

    def __init__(self, *, devices: DeviceRepository):
        self._devices = devices

    async def execute(
        self, *, query: ReadOutputPolicy, context: ActorContext
    ) -> DeviceOutputConfiguration:
        context.require_scope(POLICY_READ_SCOPE)
        current = await _owned_device(self._devices, query.device_ref, context)
        return DeviceOutputConfiguration(
            device_ref=current.device_ref,
            capabilities=manifest_outputs(json.loads(current.manifest_json)),
            policy=current.output_policy,
        )


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
        current = await _owned_device(self._devices, command.device_ref, context)
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
