from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.claim_device_pairing import ClaimDevicePairing
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 9, tzinfo=UTC)
PAIRING_SECRET = "device-local-pairing-secret-000001"


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1"),
            enrollment_id="enrollment-1",
            retrieval_token_hash="a" * 64,
            retrieval_expires_at=NOW + timedelta(minutes=10),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"}),
            enrolled_at=NOW,
            updated_at=NOW,
            identity_key_fingerprint="p256:" + "b" * 64,
            pairing_method="local-secret-sha256",
            pairing_secret_hash=("sha256:" + hashlib.sha256(PAIRING_SECRET.encode()).hexdigest()),
        )

    async def get(self, device_id):
        return self.device if device_id == self.device.identity.device_id else None

    async def get_by_enrollment_id(self, enrollment_id):
        return self.device if enrollment_id == self.device.enrollment_id else None

    async def commit(self, *, expected, device, event):
        assert self.device == expected
        self.device = device
        self.event = event
        return device


class _Projector:
    async def execute(self, device_id):
        self.device_id = device_id


def _use_case(devices):
    approve = ApproveDevice(
        devices=devices,
        mutations=devices,
        clock=_Clock(),
        handoff_ttl=timedelta(minutes=30),
        directory_projector=_Projector(),
    )
    return ClaimDevicePairing(devices=devices, approve=approve, clock=_Clock())


async def test_local_secret_binds_authenticated_owner_and_is_idempotent() -> None:
    devices = _Devices()
    claim = _use_case(devices)

    approved = await claim.execute(
        enrollment_id="enrollment-1",
        pairing_secret=PAIRING_SECRET,
        owner_id="owner-1",
        request_id="pairing-claim-1",
        principal_id="mobile-controller-1",
    )
    replay = await claim.execute(
        enrollment_id="enrollment-1",
        pairing_secret=PAIRING_SECRET,
        owner_id="owner-1",
        request_id="pairing-claim-1",
        principal_id="mobile-controller-1",
    )

    assert replay == approved
    assert approved.owner_id == "owner-1"
    assert devices.event.principal_id == "mobile-controller-1"
    assert devices.event.data["approval_method"] == "local-secret-sha256"
    devices.device = replace(devices.device, retrieval_expires_at=NOW)
    late_replay = await claim.execute(
        enrollment_id="enrollment-1",
        pairing_secret=PAIRING_SECRET,
        owner_id="owner-1",
        request_id="pairing-claim-1",
        principal_id="mobile-controller-1",
    )
    assert late_replay.owner_id == approved.owner_id
    assert late_replay.lifecycle_state == approved.lifecycle_state
    with pytest.raises(PermissionError, match="already consumed"):
        await claim.execute(
            enrollment_id="enrollment-1",
            pairing_secret=PAIRING_SECRET,
            owner_id="owner-1",
            request_id="pairing-claim-2",
            principal_id="mobile-controller-1",
        )


async def test_pairing_fails_closed_for_wrong_secret_owner_or_expiry() -> None:
    devices = _Devices()
    claim = _use_case(devices)
    with pytest.raises(PermissionError, match="invalid local"):
        await claim.execute(
            enrollment_id="enrollment-1",
            pairing_secret="attacker-local-pairing-secret-0001",
            owner_id="owner-1",
            request_id="claim-wrong",
            principal_id="mobile-1",
        )
    with pytest.raises(PermissionError, match="Owner scope"):
        await claim.execute(
            enrollment_id="enrollment-1",
            pairing_secret=PAIRING_SECRET,
            owner_id="",
            request_id="claim-no-owner",
            principal_id="mobile-1",
        )
    devices.device = replace(devices.device, retrieval_expires_at=NOW)
    with pytest.raises(ValueError, match="expired"):
        await claim.execute(
            enrollment_id="enrollment-1",
            pairing_secret=PAIRING_SECRET,
            owner_id="owner-1",
            request_id="claim-expired",
            principal_id="mobile-1",
        )
