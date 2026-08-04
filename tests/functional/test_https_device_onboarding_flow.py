from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.handoff_device import HandoffDevice
from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.contracts.bindings.onboarding import HubDescriptor
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    OpaqueChannelBinding,
)
from hub.domain.devices.entities import DeviceLifecycleState
from hub.interfaces.http.routers.device_onboarding import (
    DeviceOnboardingHttpServices,
    create_device_onboarding_router,
)

NOW = datetime(2026, 8, 4, tzinfo=UTC)
TOKEN = "device-generated-random-token-000001"


class _Clock:
    def now(self):
        return NOW


class _Ids:
    def new(self, prefix):
        return f"{prefix}-1"


class _Devices:
    def __init__(self):
        self.values = {}

    async def get(self, device_id):
        return self.values.get(device_id)

    async def get_by_enrollment_id(self, enrollment_id):
        return next(
            (d for d in self.values.values() if d.enrollment_id == enrollment_id), None
        )

    async def commit(self, *, expected, device, event):
        assert self.values.get(device.identity.device_id) == expected
        self.values[device.identity.device_id] = device
        return device


class _Projector:
    async def execute(self, device_id):
        self.device_id = device_id


class _Provider:
    async def provision_channels(self, context):
        return ChannelAssignmentSet(
            context.operation_id,
            context.device_id,
            context.manifest_revision,
            (
                ChannelGrant(
                    channel_id="channel-1",
                    device_id=context.device_id,
                    purpose="provider-selected",
                    kinds=frozenset({ChannelKind.RELIABLE_DATA}),
                    binding_format="application/reference-provider+json",
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                    opaque_binding=OpaqueChannelBinding(b"provider-secret"),
                ),
            ),
        )


def _runtime():
    devices = _Devices()
    tokens = Sha256RetrievalTokenHasher()
    services = DeviceOnboardingHttpServices(
        descriptor=HubDescriptor(
            hub_id="hub-local",
            descriptor_uri="https://hub.test/api/device-onboarding/v1/descriptor",
            device_onboarding_uri="https://hub.test/api/device-onboarding/v1",
            enrollment_uri="https://hub.test/api/device-onboarding/v1/enrollments",
        ),
        enroll=EnrollDevice(
            devices=devices,
            mutations=devices,
            clock=_Clock(),
            ids=_Ids(),
            tokens=tokens,
            enrollment_ttl=timedelta(minutes=30),
            directory_projector=_Projector(),
        ),
        handoff=HandoffDevice(
            devices=devices,
            provision=ProvisionDeviceChannels(
                hub_id="hub-local", provider=_Provider(), clock=_Clock()
            ),
            tokens=tokens,
            clock=_Clock(),
        ),
    )
    app = FastAPI()
    app.include_router(create_device_onboarding_router(services))
    return TestClient(app), devices


def _enrollment():
    return {
        "operation": "device.enrollment",
        "request_id": "enroll-1",
        "retrieval_token": TOKEN,
        "identity": {"device_id": "device-1"},
        "manifest": {"schema_version": 1, "title": "Generic Device"},
        "display_name": "Generic Device",
        "device_kind": "generic",
    }


def _handoff(token=TOKEN):
    return {
        "operation": "device.handoff",
        "request_id": "handoff-1",
        "retrieval_token": token,
    }


def test_enrollment_pending_poll_and_approved_provider_handoff() -> None:
    client, devices = _runtime()
    assert client.get("/api/device-onboarding/v1/descriptor").status_code == 200
    receipt = client.post("/api/device-onboarding/v1/enrollments", json=_enrollment())
    enrollment_id = receipt.json()["enrollment_id"]

    pending = client.post(
        f"/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff",
        json=_handoff(),
    )
    assert pending.status_code == 202
    assert pending.json()["channels"] == []

    devices.values["device-1"] = replace(
        devices.values["device-1"],
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )
    handed_off = client.post(
        f"/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff",
        json=_handoff(),
    )

    assert handed_off.status_code == 200
    assert handed_off.json()["lifecycle_state"] == "approved"
    assert base64.b64decode(handed_off.json()["channels"][0]["opaque_binding"]) == b"provider-secret"


def test_invalid_retrieval_token_cannot_claim_approved_assignment() -> None:
    client, devices = _runtime()
    enrollment_id = client.post(
        "/api/device-onboarding/v1/enrollments", json=_enrollment()
    ).json()["enrollment_id"]
    devices.values["device-1"] = replace(
        devices.values["device-1"],
        lifecycle_state=DeviceLifecycleState.APPROVED,
        owner_id="owner-1",
    )

    response = client.post(
        f"/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff",
        json=_handoff("attacker-generated-random-token-00001"),
    )
    assert response.status_code == 403


def test_removed_session_endpoints_are_not_compatible_routes() -> None:
    client, _ = _runtime()
    for endpoint in ("hello", "proof", "register", "heartbeat", "close"):
        assert client.post(f"/api/device-access/v1/{endpoint}", json={}).status_code == 404
