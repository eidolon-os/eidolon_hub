from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from jose import jwt

from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

SECRET = b"management-secret-with-at-least-32-bytes"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1", "p256:fingerprint", "local"),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
            registered_at=NOW,
            updated_at=NOW,
            owner_id="owner-1",
            approved=True,
        )

    async def get(self, device_id):
        return self.device if device_id == self.device.identity.device_id else None


def _credential(*, owner_id="owner-1", roles=None, secret=SECRET):
    token = jwt.encode(
        {
            "sub": "eidolon-agent/test",
            "owner_id": owner_id,
            "roles": roles or ["device-manager"],
            "aud": "eidolon-hub",
            "exp": int(time.time()) + 600,
        },
        secret,
        algorithm="HS256",
    )
    return f"Bearer {token}"


async def test_device_manager_is_limited_to_its_owner_and_approved_devices() -> None:
    devices = _Devices()
    authorizer = JwtOwnerManagementAuthorizer(secret=SECRET, devices=devices)

    await authorizer.authorize(credential=_credential(), owner_scope="owner-1", device_id=None)
    await authorizer.authorize(credential=_credential(), owner_scope=None, device_id="device-1")

    with pytest.raises(PermissionError, match="owner scope"):
        await authorizer.authorize(credential=_credential(), owner_scope="owner-2", device_id=None)
    devices.device = replace(devices.device, approved=False)
    with pytest.raises(PermissionError, match="cannot access"):
        await authorizer.authorize(credential=_credential(), owner_scope=None, device_id="device-1")


async def test_hub_admin_can_manage_cross_owner_and_unclaimed_scopes() -> None:
    authorizer = JwtOwnerManagementAuthorizer(secret=SECRET, devices=_Devices())
    credential = _credential(owner_id="platform", roles=["hub-admin"])

    await authorizer.authorize(credential=credential, owner_scope="unclaimed", device_id=None)
    await authorizer.authorize(credential=credential, owner_scope=None, device_id="device-1")


async def test_invalid_signature_or_missing_role_is_rejected() -> None:
    authorizer = JwtOwnerManagementAuthorizer(secret=SECRET, devices=_Devices())

    with pytest.raises(PermissionError, match="invalid"):
        await authorizer.authorize(
            credential=_credential(secret=b"another-secret-with-at-least-32-bytes"),
            owner_scope="owner-1",
            device_id=None,
        )
    with pytest.raises(PermissionError, match="role"):
        await authorizer.authorize(
            credential=_credential(roles=["viewer"]),
            owner_scope="owner-1",
            device_id=None,
        )
