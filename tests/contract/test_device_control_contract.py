from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hub.contracts.bindings.common import DeviceIdentity
from hub.contracts.bindings.device import DeviceDirectoryEntry, DeviceManifest

NOW = datetime(2026, 8, 4, tzinfo=UTC)


def test_device_identity_contains_only_stable_device_id() -> None:
    identity = DeviceIdentity(device_id="device-1")
    assert identity.model_dump() == {"device_id": "device-1"}
    with pytest.raises(ValidationError):
        DeviceIdentity.model_validate(
            {"device_id": "device-1", "tenant_id": "device-selected-scope"}
        )


def test_public_directory_exposes_no_session_online_or_retrieval_facts() -> None:
    entry = DeviceDirectoryEntry(
        device_id="device-1",
        owner_scope="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest=DeviceManifest(title="Device"),
        manifest_revision="sha256:revision",
        lifecycle_state="approved",
        enrolled_at=NOW,
        updated_at=NOW,
    )
    payload = entry.model_dump(mode="json")

    assert payload["manifest"]["title"] == "Device"
    for forbidden in (
        "session_id", "online", "heartbeat", "retrieval_token", "retrieval_token_hash"
    ):
        assert forbidden not in payload
