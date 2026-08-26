from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from hub.application.queries.get_device import GetDevice
from hub.contracts.mappers import directory_entry_to_wire
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    DeviceLifecycleState,
)
from hub.domain.devices.manifest import DeviceManifestDocument

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_10_51_DB_7E_24_44 = named_device_instance_id("10:51:db:7e:24:44")

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _entry(
    device_id: str,
    *,
    lifecycle_state: DeviceLifecycleState = DeviceLifecycleState.APPROVED,
) -> DeviceDirectoryEntry:
    return DeviceDirectoryEntry(
        device_id=device_id,
        owner_scope="owner-1",
        display_name=f"Display {device_id}",
        device_kind="display",
        manifest=DeviceManifestDocument.from_declaration(document=
            {
                "schema_version": 1,
                "title": "Display",
                "actions": [
                    {
                        "name": "display.render",
                        "version": 1,
                        "input_schema": {},
                        "output_schema": {},
                    }
                ],
            }
        , declared_revision=1),
        lifecycle_state=lifecycle_state,
        claim_generation=1,
        trust_epoch=1,
        enrolled_at=NOW,
        updated_at=NOW,
    )


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _Directory:
    def __init__(self) -> None:
        self.entries = (
            _entry("device-a"),
            replace(_entry("device-b"), device_kind="sensor"),
            _entry("device-c"),
        )

    async def get(self, *, owner_scope, device_id):
        return next(
            (
                entry
                for entry in self.entries
                if entry.owner_scope == owner_scope and entry.device_id == device_id
            ),
            None,
        )

    async def list(self, *, owner_scope):
        return tuple(entry for entry in self.entries if entry.owner_scope == owner_scope)


def test_directory_wire_accepts_deployed_mac_style_device_id() -> None:
    entry = replace(
        _entry(_10_51_DB_7E_24_44),
        owner_scope="business_owner_account_1",
        owner_domain_id="owner-domain_01",
    )
    wire = directory_entry_to_wire(entry)

    assert wire.device_ref is not None
    assert wire.device_ref.device_instance_id == _10_51_DB_7E_24_44
    assert str(wire.device_ref.owner_domain_id) == "owner-domain_01"
    assert str(wire.device_ref.owner_domain_id) != wire.owner_scope


@pytest.mark.asyncio
async def test_get_device_is_owner_scoped_and_exact() -> None:
    query = GetDevice(_Directory())

    assert (
        await query.execute(owner_scope="owner-1", device_id="device-a")
    ).device_id == "device-a"
    with pytest.raises(KeyError):
        await query.execute(owner_scope="other-owner", device_id="device-a")
