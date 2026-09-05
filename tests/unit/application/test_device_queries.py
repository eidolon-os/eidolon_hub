from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from hub.application.queries.get_device import GetDevice
from hub.contracts.bindings.device import ForeignDeviceManifest
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
        manifest_id="display",
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
            replace(_entry("device-b"), manifest_id="sensor"),
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


def test_a_document_older_than_this_vocabulary_projects_instead_of_raising() -> None:
    """The other direction: history the entry would refuse today.

    `test_the_directory_reads_every_document_the_entry_admits` covers documents
    a device could still assert. This covers the ones already in the table. The
    entry has widened over time, so a document the Authority accepted need not
    match today's vocabulary at all — the first device ever claimed canonically
    sent `{"endpoints": []}` — and reading one used to raise, which the Owner
    met as a bare 500 on a device Hub itself had admitted.

    So it projects as the foreign document it is: the row still names the
    device, its lifecycle and the digest of what the Authority holds, and says
    plainly that the capability declaration is not one this vocabulary reads.
    Not `null`, which cannot be told apart from a device that declares nothing,
    and not a partial parse, which would report an empty capability set for a
    device that has one.
    """

    entry = replace(
        _entry(_10_51_DB_7E_24_44),
        manifest=DeviceManifestDocument.from_declaration(
            document={"endpoints": []}, declared_revision=1
        ),
    )

    wire = directory_entry_to_wire(entry)

    assert isinstance(wire.manifest, ForeignDeviceManifest)
    assert wire.manifest.manifest_kind == "foreign"
    assert "endpoints" in wire.manifest.detail and "title" in wire.manifest.detail
    # The parts an Owner acts on survive: which device, whether it is approved,
    # and exactly which document the Authority is holding.
    assert wire.device_id == _10_51_DB_7E_24_44
    assert wire.lifecycle_state == "approved"
    assert wire.manifest_revision == entry.manifest.digest


def test_the_directory_reads_every_document_the_entry_admits() -> None:
    """One definition upstream, and a reader here that cannot be narrower.

    The Manifest had five definitions and no agreement between them: this
    binding made `codecs` optional while the Channel Provider required it, so
    one document could pass the Authority and be refused at provisioning —
    an approved Claim whose channel never arrived. The definition now lives
    once, in `eidolon_sdk`'s `DeviceCapabilityManifest`, and both entry points
    check a proposed document against it.

    What must hold here is the direction: every document the entry admits must
    project, or the Owner cannot see a device the Authority accepted. Driven
    from the golden vectors, so renaming a field or changing an enum in the
    canonical vocabulary turns this red rather than being discovered on a
    device page.
    """

    import json
    from pathlib import Path

    import eidolon_sdk

    from hub.contracts.bindings.device import DeviceManifest

    # Located from the installed SDK rather than by walking up from here, so
    # this reads the same corpus the entry gate is checked against wherever
    # that package comes from.
    vectors = (
        Path(eidolon_sdk.__file__).resolve().parents[1]
        / "contracts/device_foundation/v1/examples/valid/common.json"
    )
    assert vectors.exists(), f"the canonical contract corpus is not at {vectors}"
    cases = [
        case
        for case in json.loads(vectors.read_text(encoding="utf-8"))["cases"]
        if case["definition"] == "DeviceCapabilityManifest"
    ]
    assert len(cases) >= 5, "the canonical Manifest vectors are missing"

    for case in cases:
        try:
            DeviceManifest.model_validate(case["value"])
        except Exception as exc:  # noqa: BLE001 - the message is the point
            raise AssertionError(
                f"{case['case_id']} is admissible at the entry but does not project: {exc}"
            ) from exc
