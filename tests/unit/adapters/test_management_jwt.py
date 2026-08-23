from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from jose import jwt

from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.identity import ManagementPermission

SECRET = b"management-secret-with-at-least-32-bytes"
REGISTRY_READER_TOKEN = "device-registry-reader-token-000001"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1"),
            enrollment_id="enrollment-1",
            retrieval_token_hash="a" * 64,
            retrieval_expires_at=NOW,
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
            enrolled_at=NOW,
            updated_at=NOW,
            owner_id="owner-1",
            lifecycle_state=DeviceLifecycleState.APPROVED,
        )

    async def get(self, device_id):
        return self.device if device_id == self.device.identity.device_id else None


def _credential(*, owner_id="owner-1", roles=None, scopes=None, secret=SECRET):
    token = jwt.encode(
        {
            "sub": "eidolon-agent/test",
            "owner_id": owner_id,
            "roles": roles or ["device-manager"],
            "scopes": scopes
            or [
                "device.read",
                "device.claim.approve",
                "device.claim.revoke",
                "device.events.read",
            ],
            "aud": "eidolon-admission",
            "exp": int(time.time()) + 600,
        },
        secret,
        algorithm="HS256",
    )
    return f"Bearer {token}"


async def test_device_manager_is_limited_to_its_owner_and_approved_devices() -> None:
    devices = _Devices()
    authorizer = JwtOwnerManagementAuthorizer(
        secret=SECRET,
        devices=devices,
        device_registry_reader_token=REGISTRY_READER_TOKEN,
    )

    principal = await authorizer.authorize(
        credential=_credential(),
        permission=ManagementPermission.DEVICE_LIST,
        owner_scope="owner-1",
        device_id=None,
    )
    await authorizer.authorize(
        credential=_credential(),
        permission=ManagementPermission.DEVICE_REVOKE,
        owner_scope=None,
        device_id="device-1",
    )

    assert principal.subject_id == "eidolon-agent/test"
    assert principal.owner_id == "owner-1"
    assert principal.roles == frozenset({"device-manager"})

    with pytest.raises(PermissionError, match="owner scope"):
        await authorizer.authorize(
            credential=_credential(),
            permission=ManagementPermission.DEVICE_LIST,
            owner_scope="owner-2",
            device_id=None,
        )
    devices.device = replace(
        devices.device,
        owner_id=None,
        lifecycle_state=DeviceLifecycleState.PENDING_APPROVAL,
    )
    with pytest.raises(PermissionError, match="cannot access"):
        await authorizer.authorize(
            credential=_credential(),
            permission=ManagementPermission.DEVICE_APPROVE,
            owner_scope=None,
            device_id="device-1",
        )


async def test_hub_admin_can_manage_cross_owner_and_unclaimed_scopes() -> None:
    authorizer = JwtOwnerManagementAuthorizer(
        secret=SECRET,
        devices=_Devices(),
        device_registry_reader_token=REGISTRY_READER_TOKEN,
    )
    credential = _credential(owner_id="platform", roles=["hub-admin"])

    await authorizer.authorize(
        credential=credential,
        permission=ManagementPermission.DEVICE_LIST,
        owner_scope="unclaimed",
        device_id=None,
    )
    await authorizer.authorize(
        credential=credential,
        permission=ManagementPermission.DEVICE_APPROVE,
        owner_scope=None,
        device_id="device-1",
    )


async def test_device_registry_reader_can_only_read_one_exact_device() -> None:
    authorizer = JwtOwnerManagementAuthorizer(
        secret=SECRET,
        devices=_Devices(),
        device_registry_reader_token=REGISTRY_READER_TOKEN,
    )
    credential = f"Bearer {REGISTRY_READER_TOKEN}"

    principal = await authorizer.authorize(
        credential=credential,
        permission=ManagementPermission.DEVICE_GET,
        owner_scope="owner-1",
        device_id="device-1",
    )

    assert principal.roles == frozenset({"device-registry-reader"})
    for permission in (
        ManagementPermission.DEVICE_LIST,
        ManagementPermission.DEVICE_EVENTS,
        ManagementPermission.DEVICE_APPROVE,
        ManagementPermission.DEVICE_REVOKE,
    ):
        with pytest.raises(PermissionError, match="exact reads"):
            await authorizer.authorize(
                credential=credential,
                permission=permission,
                owner_scope="owner-1",
                device_id="device-1",
            )


async def test_invalid_signature_or_missing_role_is_rejected() -> None:
    authorizer = JwtOwnerManagementAuthorizer(
        secret=SECRET,
        devices=_Devices(),
        device_registry_reader_token=REGISTRY_READER_TOKEN,
    )

    with pytest.raises(PermissionError, match="invalid"):
        await authorizer.authorize(
            credential=_credential(secret=b"another-secret-with-at-least-32-bytes"),
            permission=ManagementPermission.DEVICE_LIST,
            owner_scope="owner-1",
            device_id=None,
        )
    with pytest.raises(PermissionError, match="role"):
        await authorizer.authorize(
            credential=_credential(roles=["viewer"]),
            permission=ManagementPermission.DEVICE_LIST,
            owner_scope="owner-1",
            device_id=None,
        )
