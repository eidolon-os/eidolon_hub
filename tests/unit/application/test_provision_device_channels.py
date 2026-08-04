from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    OpaqueChannelBinding,
)
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 4, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Provider:
    def __init__(self):
        self.contexts = []

    async def provision_channels(self, context):
        self.contexts.append(context)
        return ChannelAssignmentSet(
            operation_id=context.operation_id,
            device_id=context.device_id,
            manifest_revision=context.manifest_revision,
            grants=(
                ChannelGrant(
                    channel_id="channel-1",
                    device_id=context.device_id,
                    purpose="provider-selected-purpose",
                    kinds=frozenset({ChannelKind.VIDEO}),
                    binding_format="application/vnd.eidolon.channel-binding+json",
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                    opaque_binding=OpaqueChannelBinding(b"provider-owned-binding"),
                ),
            ),
        )


def _device(*, state=DeviceLifecycleState.APPROVED):
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        enrollment_id="enrollment-1",
        retrieval_token_hash="a" * 64,
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Device"}
        ),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner-1" if state is DeviceLifecycleState.APPROVED else None,
        lifecycle_state=state,
    )


async def test_provision_relays_provider_assignment_without_local_state() -> None:
    provider = _Provider()
    assignments = await ProvisionDeviceChannels(
        hub_id="hub-1", provider=provider, clock=_Clock()
    ).execute(device=_device(), operation_id="enrollment-1")

    assert provider.contexts[0].manifest_json == _device().manifest_json
    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned-binding"
    assert "provider-owned-binding" not in repr(assignments)


@pytest.mark.parametrize(
    "state", (DeviceLifecycleState.PENDING_APPROVAL, DeviceLifecycleState.REVOKED)
)
async def test_provision_rejects_nonapproved_device(state) -> None:
    provider = _Provider()
    with pytest.raises(PermissionError, match="approved"):
        await ProvisionDeviceChannels(
            hub_id="hub-1", provider=provider, clock=_Clock()
        ).execute(device=_device(state=state), operation_id="enrollment-1")
    assert provider.contexts == []


@pytest.mark.parametrize("operation_id", ("", "x" * 97))
async def test_provision_requires_bounded_operation_id(operation_id) -> None:
    with pytest.raises(ValueError, match="operation_id"):
        await ProvisionDeviceChannels(
            hub_id="hub-1", provider=_Provider(), clock=_Clock()
        ).execute(device=_device(), operation_id=operation_id)


@pytest.mark.parametrize("mutation", ("identity", "expired", "grant-device"))
async def test_invalid_provider_assignments_are_rejected(mutation) -> None:
    class _InvalidProvider(_Provider):
        async def provision_channels(self, context):
            response = await super().provision_channels(context)
            if mutation == "identity":
                return replace(response, device_id="another-device")
            grant = (
                replace(
                    response.grants[0],
                    issued_at=NOW - timedelta(minutes=2),
                    expires_at=NOW - timedelta(minutes=1),
                )
                if mutation == "expired"
                else replace(response.grants[0], device_id="another-device")
            )
            return replace(response, grants=(grant,))

    with pytest.raises(ValueError):
        await ProvisionDeviceChannels(
            hub_id="hub-1", provider=_InvalidProvider(), clock=_Clock()
        ).execute(device=_device(), operation_id="enrollment-1")
