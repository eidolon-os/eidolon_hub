from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _manifest() -> DeviceManifestDocument:
    return DeviceManifestDocument.from_declaration(document={"schema_version": 1, "title": "Device"}, declared_revision=1)


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
        DeviceManifestDocument.from_declaration(document=[], declared_revision=1)
    with pytest.raises(ValueError, match="valid JSON"):
        DeviceManifestDocument("{", "sha256:bad", 1)
    with pytest.raises(ValueError, match="schema_version"):
        DeviceManifestDocument.from_declaration(document={"schema_version": 2}, declared_revision=1)
    # A canonical Manifest is opaque to the Authority that accepted it, and the
    # owner-facing directory only projects it. Requiring this vocabulary's own
    # version field of a document authored elsewhere made a projection row able
    # to kill Hub at startup — for a Manifest it had already admitted.
    canonical = DeviceManifestDocument.from_declaration(document={"endpoints": []}, declared_revision=1)
    assert canonical.canonical_json == '{"endpoints":[]}'
    assert not canonical.declares_capability("anything")
    with pytest.raises(ValueError, match="digest"):
        DeviceManifestDocument(manifest.canonical_json, "sha256:bad", 1)
    # The device's own count of its capability changes is not a digest, and a
    # document with no such count has no place in the ordering of assertions.
    with pytest.raises(ValueError, match="declared_revision"):
        DeviceManifestDocument.from_declaration(
            document={"schema_version": 1}, declared_revision=0
        )


def test_managed_device_policy_invariants() -> None:
    device = _device()
    assert device.manifest_json == device.manifest.canonical_json
    with pytest.raises(ValueError, match="device_kind"):
        replace(device, device_kind="")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(device, updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="requires an owner"):
        replace(device, owner_id=None)
