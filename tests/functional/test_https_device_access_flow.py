from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.application.use_cases.acquire_device_channels import AcquireDeviceChannels
from hub.application.use_cases.close_session import CloseDeviceSession
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_session import RenewDeviceSession
from hub.contracts.bindings.session import HubDescriptor
from hub.domain.channels.entities import (
    ChannelAssignmentSet,
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    OpaqueChannelBinding,
)
from hub.domain.sessions.entities import DeviceAuthorityLease
from hub.interfaces.http.routers.device_access import (
    DeviceAccessHttpServices,
    create_device_access_router,
)

NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Ids:
    sequence = 0

    def new(self, prefix):
        self.sequence += 1
        return f"{prefix}-{self.sequence:016d}"


class _Challenges:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def create(self, value):
        self.values[value.challenge_id] = value

    async def consume(self, key):
        value = replace(self.values[key], consumed=True)
        self.values[key] = value
        return value


class _Proof:
    async def verify(self, **kwargs):
        return "p256:functional-fingerprint"


class _Credentials:
    def issue_lease_token(self, *, session_id, device_id):
        return f"signed-session-token-{device_id}"


class _Authority:
    async def acquire(self, *, device_id, hub_instance_id, now, ttl):
        self.value = DeviceAuthorityLease(device_id, hub_instance_id, 1, now + ttl)
        return self.value

    async def renew(self, **values):
        self.value = replace(self.value, expires_at=values["now"] + values["ttl"])
        return self.value

    async def validate(self, **kwargs):
        return self.value


class _Sessions:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, value):
        self.values[value.session_id] = value
        return value

    async def active_for_device(self, device_id, *, now):
        return tuple(
            value
            for value in self.values.values()
            if value.device_id == device_id and value.is_active(now)
        )


class _Devices:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, value):
        self.values[value.identity.device_id] = value
        return value


class _Events:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)


class _Channels:
    def __init__(self):
        self.values = {}

    async def upsert(self, value):
        self.values[value.channel_id] = value
        return value

    async def list_for_device(self, device_id):
        return tuple(value for value in self.values.values() if value.device_id == device_id)


class _Provider:
    async def acquire_channels(self, context):
        lease = ChannelLease(
            channel_id="channel-1",
            device_id=context.device_id,
            purpose="management",
            kinds=frozenset({ChannelKind.RELIABLE_DATA}),
            binding_format="application/reference-provider+json",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        return ChannelAssignmentSet(
            context.operation_id,
            context.device_id,
            context.manifest_revision,
            (ChannelGrant(context.operation_id, lease, OpaqueChannelBinding(b"provider-secret")),),
        )


def _runtime():
    sessions, devices, events, channels = _Sessions(), _Devices(), _Events(), _Channels()
    authority = _Authority()
    enroll = EnrollDevice(
        challenges=_Challenges(),
        sessions=sessions,
        authority=authority,
        proof_verifier=_Proof(),
        credential_issuer=_Credentials(),
        clock=_Clock(),
        ids=_Ids(),
        hub_instance_id="hub-local-1",
    )
    services = DeviceAccessHttpServices(
        descriptor=HubDescriptor(
            hub_id="hub-local",
            descriptor_uri="https://hub.test/api/device-access/v1/descriptor",
            device_access_uri="https://hub.test/api/device-access/v1",
            registration_uri="https://hub.test/api/device-access/v1/register",
            channel_acquisition_uri="https://hub.test/api/device-access/v1/channels/acquire",
        ),
        enroll=enroll,
        register=RegisterDevice(devices=devices, sessions=sessions, events=events, clock=_Clock()),
        renew=RenewDeviceSession(
            sessions=sessions,
            authority=authority,
            events=events,
            clock=_Clock(),
            ttl=timedelta(seconds=45),
        ),
        close=CloseDeviceSession(sessions=sessions, events=events, clock=_Clock()),
        acquire_channels=AcquireDeviceChannels(
            hub_id="hub-local",
            devices=devices,
            sessions=sessions,
            channels=channels,
            provider=_Provider(),
            clock=_Clock(),
        ),
    )
    app = FastAPI()
    app.include_router(create_device_access_router(services))
    return TestClient(app), devices, sessions, channels, events


def _register_payload(session):
    return {
        "operation": "session.registration",
        "session_id": session["session_id"],
        "lease_token": session["lease_token"],
        "registration": {
            "operation": "device.registration",
            "request_id": "request-register",
            "identity": {
                "device_id": "device-1",
                "public_key_fingerprint": "p256:functional-fingerprint",
                "tenant_id": "local",
            },
            "manifest": {"schema_version": 1, "title": "Generic Device"},
            "display_name": "Generic Device",
            "device_kind": "generic",
        },
    }


def test_https_session_registration_heartbeat_and_channel_acquisition() -> None:
    client, devices, _sessions, channels, events = _runtime()
    descriptor = client.get("/api/device-access/v1/descriptor")
    assert descriptor.status_code == 200
    hello = client.post(
        "/api/device-access/v1/hello",
        json={
            "operation": "session.hello",
            "request_id": "request-hello",
            "device_id": "device-1",
            "client_nonce": "0123456789abcdef",
        },
    )
    proof = client.post(
        "/api/device-access/v1/proof",
        json={
            "operation": "session.proof",
            "request_id": "request-proof",
            "challenge_id": hello.json()["challenge_id"],
            "device_id": "device-1",
            "public_key": "p" * 32,
            "signature": "s" * 32,
        },
    )
    assert proof.status_code == 200
    session = proof.json()
    assert (
        client.post("/api/device-access/v1/register", json=_register_payload(session)).status_code
        == 200
    )
    devices.values["device-1"] = replace(
        devices.values["device-1"], approved=True, owner_id="owner-1"
    )

    heartbeat = client.post(
        "/api/device-access/v1/heartbeat",
        json={
            "operation": "session.heartbeat",
            "request_id": "request-heartbeat",
            "session_id": session["session_id"],
            "lease_token": session["lease_token"],
            "sequence": 1,
        },
    )
    acquired = client.post(
        "/api/device-access/v1/channels/acquire",
        json={
            "operation": "channel.acquire",
            "request_id": "request-acquire",
            "session_id": session["session_id"],
            "lease_token": session["lease_token"],
        },
    )

    assert heartbeat.status_code == 200
    assert acquired.status_code == 200, acquired.text
    assert base64.b64decode(acquired.json()["channels"][0]["opaque_binding"]) == b"provider-secret"
    assert channels.values["channel-1"].binding_format == "application/reference-provider+json"
    assert len(events.values) == 2


def test_consumed_challenge_and_invalid_session_credential_fail_closed() -> None:
    client, _devices, _sessions, _channels, _events = _runtime()
    hello = client.post(
        "/api/device-access/v1/hello",
        json={
            "operation": "session.hello",
            "request_id": "hello-1",
            "device_id": "device-1",
            "client_nonce": "0123456789abcdef",
        },
    ).json()
    payload = {
        "operation": "session.proof",
        "request_id": "proof-1",
        "challenge_id": hello["challenge_id"],
        "device_id": "device-1",
        "public_key": "p" * 32,
        "signature": "s" * 32,
    }
    accepted = client.post("/api/device-access/v1/proof", json=payload).json()

    assert client.post("/api/device-access/v1/proof", json=payload).status_code == 401
    registration = _register_payload(accepted)
    registration["lease_token"] = "wrong-token-0000000000000000"
    assert client.post("/api/device-access/v1/register", json=registration).status_code == 401
