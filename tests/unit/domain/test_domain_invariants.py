from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _manifest() -> DeviceManifestDocument:
    return DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"})


def _device() -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity("device-1"),
        display_name="Device",
        device_kind="generic",
        manifest=_manifest(),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_id="owner-1",
    )


def test_identity_and_manifest_values() -> None:
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


def test_managed_device_policy_invariants() -> None:
    device = _device()
    assert device.manifest_json == device.manifest.canonical_json
    with pytest.raises(ValueError, match="device_kind"):
        replace(device, device_kind="")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(device, updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="requires an owner"):
        replace(device, owner_id=None)
