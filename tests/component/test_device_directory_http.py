"""What an Owner is told about a device whose Manifest predates this vocabulary."""

from __future__ import annotations

from datetime import UTC, datetime

from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.application.queries.get_device import GetDevice
from hub.domain.devices.entities import DeviceDirectoryEntry, DeviceLifecycleState
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.interfaces.http.routers.device_management import (
    DeviceManagementHttpServices,
    create_device_management_router,
)
from hub.ports.identity import ManagementPrincipal

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_10_51_DB_7E_24_44 = named_device_instance_id("10:51:db:7e:24:44")

NOW = datetime(2026, 9, 6, tzinfo=UTC)

#: What the first device ever claimed canonically actually sent. The entry that
#: admitted it typed the document `{"type": "object"}`; today's would refuse it.
INCIDENT_DOCUMENT = {"endpoints": []}


class _Authorizer:
    async def authorize(self, *, credential, permission, owner_scope, device_id):
        return ManagementPrincipal(
            subject_id="owner-operator", owner_id=owner_scope, roles=frozenset({"owner"})
        )


class _Directory:
    def __init__(self, entry: DeviceDirectoryEntry) -> None:
        self._entry = entry

    async def get(self, *, owner_scope: str, device_id: str):
        if (owner_scope, device_id) == (self._entry.owner_scope, self._entry.device_id):
            return self._entry
        return None


def _entry(document: dict) -> DeviceDirectoryEntry:
    return DeviceDirectoryEntry(
        device_id=_10_51_DB_7E_24_44,
        owner_scope="business_owner_account_1",
        owner_domain_id="owner-domain_01",
        display_name="Kitchen display",
        manifest_id="display",
        manifest=DeviceManifestDocument.from_declaration(
            document=document, declared_revision=1
        ),
        lifecycle_state=DeviceLifecycleState.APPROVED,
        claim_generation=1,
        trust_epoch=1,
        enrolled_at=NOW,
        updated_at=NOW,
    )


def _get(document: dict):
    app = FastAPI()
    app.include_router(
        create_device_management_router(
            services=DeviceManagementHttpServices(
                get_device=GetDevice(_Directory(_entry(document))),
                authorizer=_Authorizer(),
            )
        )
    )
    return TestClient(app).get(
        f"/api/device-management/v1/owners/business_owner_account_1"
        f"/devices/{_10_51_DB_7E_24_44}",
        headers={"Authorization": "Bearer owner-token"},
    )


def test_owner_can_read_a_device_whose_manifest_this_vocabulary_cannot() -> None:
    """A read of stored history must not fail on what the Authority admitted.

    This was a 500 with no problem body: the directory parsed the stored
    document against the canonical vocabulary, `extra="forbid"` and a required
    `title` refused it, and the Owner lost the whole device — not just its
    capabilities — for a device Hub had accepted and an Owner had approved.
    """

    response = _get(INCIDENT_DOCUMENT)

    assert response.status_code == 200
    body = response.json()
    assert body["manifest"] == {
        "manifest_kind": "foreign",
        "detail": "endpoints: Extra inputs are not permitted; title: Field required",
    }
    # The device is still a device: the row an Owner acts on is intact, and the
    # digest still names exactly which document the Authority is holding.
    assert body["device_id"] == _10_51_DB_7E_24_44
    assert body["display_name"] == "Kitchen display"
    assert body["lifecycle_state"] == "approved"
    assert body["manifest_revision"].startswith("sha256:")


def test_a_readable_manifest_is_still_projected_as_itself() -> None:
    """The foreign case is a fallback, not a new shape for every device."""

    response = _get({"schema_version": 1, "title": "Kitchen display"})

    assert response.status_code == 200
    assert response.json()["manifest"] == {
        "schema_version": 1,
        "title": "Kitchen display",
        "properties": [],
        "actions": [],
        "events": [],
        "media": [],
    }
