from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.domain.channels.entities import (
    ChannelGrant,
    ChannelKind,
    OpaqueChannelBinding,
    ProviderChannelRevocation,
    ProviderDeviceContext,
)
from hub.domain.devices.entities import (
    DeviceEnrollmentIntent,
    DeviceLifecycleState,
    ManagedDevice,
)
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _manifest() -> DeviceManifestDocument:
    return DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"})


def _device() -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        enrollment_id="enrollment-1",
        retrieval_token_hash="a" * 64,
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Device",
        device_kind="generic",
        manifest=_manifest(),
        enrolled_at=NOW,
        updated_at=NOW,
    )


def _channel_grant() -> ChannelGrant:
    return ChannelGrant(
        channel_id="channel-1",
        device_id="device-1",
        purpose="management",
        kinds=frozenset({ChannelKind.RELIABLE_DATA}),
        binding_format="application/eidolon-test+json",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        opaque_binding=OpaqueChannelBinding(b"encrypted"),
    )


def test_channel_binding_and_grant_invariants() -> None:
    with pytest.raises(ValueError, match="required"):
        OpaqueChannelBinding(b"")
    with pytest.raises(ValueError, match="64KiB"):
        OpaqueChannelBinding(b"x" * (64 * 1024 + 1))
    binding = OpaqueChannelBinding(b"encrypted")
    assert binding.relay_bytes() == b"encrypted"

    grant = _channel_grant()
    for changes, message in (
        ({"channel_id": ""}, "required"),
        ({"kinds": frozenset()}, "at least one"),
        ({"binding_format": ""}, "binding_format"),
        ({"issued_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"expires_at": NOW}, "after issued_at"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(grant, **changes)


def test_provider_control_values_are_bounded() -> None:
    context = ProviderDeviceContext(
        operation_id="enrollment-1",
        hub_id="hub-1",
        device_id="device-1",
        owner_id="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json='{"schema_version":1}',
        manifest_revision="sha256:manifest",
    )
    with pytest.raises(ValueError, match="identifiers"):
        replace(context, hub_id="")
    with pytest.raises(ValueError, match="owner_id"):
        replace(context, owner_id="")
    revocation = ProviderChannelRevocation("revoke-1", "hub-1", "device-1", "operator")
    with pytest.raises(ValueError, match="identifiers"):
        replace(revocation, device_id="")
    with pytest.raises(ValueError, match="reason"):
        replace(revocation, reason="")


def test_identity_manifest_and_enrollment_values() -> None:
    with pytest.raises(ValueError, match="device_id"):
        DeviceIdentity("")
    manifest = _manifest()
    assert manifest.canonical_json == '{"schema_version":1,"title":"Device"}'
    with pytest.raises(ValueError, match="object"):
        DeviceManifestDocument.from_mapping([])
    with pytest.raises(ValueError, match="valid JSON"):
        DeviceManifestDocument("{", "sha256:bad")
    with pytest.raises(ValueError, match="schema_version"):
        DeviceManifestDocument.from_mapping({"schema_version": 2})
    with pytest.raises(ValueError, match="revision"):
        DeviceManifestDocument(manifest.canonical_json, "sha256:bad")
    DeviceEnrollmentIntent(
        "request-1", "device-generated-random-token-000001", DeviceIdentity("device-1"),
        "Device", "generic", manifest,
    )


def test_managed_device_policy_invariants() -> None:
    device = _device()
    assert device.manifest_json == device.manifest.canonical_json
    with pytest.raises(ValueError, match="enrollment_id"):
        replace(device, enrollment_id="")
    with pytest.raises(ValueError, match="device_kind"):
        replace(device, device_kind="")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(device, updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="requires an owner"):
        replace(device, lifecycle_state=DeviceLifecycleState.APPROVED)
    with pytest.raises(ValueError, match="pending device"):
        replace(device, owner_id="owner-1")
