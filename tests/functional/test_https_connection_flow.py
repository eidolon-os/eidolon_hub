from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.adapters.connections.http import (
    HttpConnectionServices,
    HttpSignalMailbox,
    create_connection_router,
)
from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.contracts.bindings.connection import HubDescriptor
from hub.domain.connections.entities import DeviceAuthorityLease


class _Clock:
    value = datetime(2026, 8, 1, tzinfo=UTC)

    def now(self):
        return self.value


class _Ids:
    def __init__(self):
        self.sequence = 0

    def new(self, prefix: str):
        self.sequence += 1
        return f"{prefix}-{self.sequence:016d}"


class _Challenges:
    def __init__(self):
        self.values = {}

    async def get(self, challenge_id):
        return self.values.get(challenge_id)

    async def create(self, challenge):
        if challenge.challenge_id in self.values:
            raise ValueError("duplicate challenge")
        self.values[challenge.challenge_id] = challenge

    async def consume(self, challenge_id):
        current = self.values[challenge_id]
        if current.consumed:
            raise PermissionError("challenge already consumed")
        consumed = replace(current, consumed=True)
        self.values[challenge_id] = consumed
        return consumed


class _ProofVerifier:
    async def verify(self, *, challenge, public_key, signature):
        assert challenge.client_nonce == "0123456789abcdef"
        return "p256:functional-fingerprint"


class _Credentials:
    def issue_lease_token(self, *, connection_id, device_id):
        return f"signed-lease-token-{device_id}"


class _Authority:
    async def acquire(self, *, device_id, hub_instance_id, now, ttl):
        self.lease = DeviceAuthorityLease(
            device_id=device_id,
            hub_instance_id=hub_instance_id,
            fencing_token=1,
            expires_at=now + ttl,
        )
        return self.lease

    async def renew(self, *, device_id, hub_instance_id, fencing_token, now, ttl):
        return await self.acquire(
            device_id=device_id, hub_instance_id=hub_instance_id, now=now, ttl=ttl
        )

    async def validate(self, **kwargs):
        return self.lease


class _Connections:
    def __init__(self):
        self.values = {}

    async def get(self, connection_id):
        return self.values.get(connection_id)

    async def upsert(self, lease):
        self.values[lease.connection_id] = lease
        return lease

    async def active_for_device(self, device_id, *, now):
        return tuple(
            lease
            for lease in self.values.values()
            if lease.device_id == device_id and lease.is_active(now)
        )


class _Devices:
    def __init__(self):
        self.values = {}

    async def get(self, device_id):
        return self.values.get(device_id)

    async def upsert(self, device):
        self.values[device.identity.device_id] = device
        return device

    async def list_all(self):
        return tuple(self.values.values())


class _Events:
    def __init__(self):
        self.values = []

    async def publish(self, event):
        self.values.append(event)


def _client():
    clock = _Clock()
    connections = _Connections()
    authority = _Authority()
    devices = _Devices()
    events = _Events()
    enroll = EnrollDevice(
        challenges=_Challenges(),
        connections=connections,
        authority=authority,
        proof_verifier=_ProofVerifier(),
        credential_issuer=_Credentials(),
        clock=clock,
        ids=_Ids(),
        hub_instance_id="hub-local",
    )
    register = RegisterDevice(devices=devices, connections=connections, events=events, clock=clock)
    renew = RenewConnection(
        connections=connections,
        authority=authority,
        events=events,
        clock=clock,
        ttl=timedelta(seconds=45),
    )
    authenticate_connection = AuthenticateConnection(connections=connections, clock=clock)
    descriptor = HubDescriptor(
        hub_id="hub-local",
        descriptor_uri="https://hub.test/api/connection/v1/descriptor",
        https_registration_uri="https://hub.test/api/connection/v1/register",
    )
    app = FastAPI()
    app.include_router(
        create_connection_router(
            HttpConnectionServices(
                descriptor=descriptor,
                enroll=enroll,
                register=register,
                renew=renew,
                mailbox=HttpSignalMailbox(),
                authenticate_connection=authenticate_connection,
            )
        )
    )
    return TestClient(app), devices, connections, events


def test_https_hello_proof_registration_and_heartbeat_use_one_contract() -> None:
    client, devices, connections, events = _client()
    hello = client.post(
        "/api/connection/v1/hello",
        json={
            "operation": "connection.hello",
            "request_id": "request-hello",
            "device_id": "device-1",
            "connector_id": "https-local",
            "client_nonce": "0123456789abcdef",
        },
    )
    assert hello.status_code == 200

    proof = client.post(
        "/api/connection/v1/proof",
        json={
            "operation": "connection.proof",
            "request_id": "request-proof",
            "challenge_id": hello.json()["challenge_id"],
            "device_id": "device-1",
            "public_key": "p" * 32,
            "signature": "s" * 32,
        },
    )
    assert proof.status_code == 200
    accepted = proof.json()
    replay = client.post(
        "/api/connection/v1/proof",
        json={
            "operation": "connection.proof",
            "request_id": "request-proof-replay",
            "challenge_id": hello.json()["challenge_id"],
            "device_id": "device-1",
            "public_key": "p" * 32,
            "signature": "s" * 32,
        },
    )
    assert replay.status_code == 401

    registration = client.post(
        "/api/connection/v1/register",
        json={
            "operation": "connection.registration",
            "connection_id": accepted["connection_id"],
            "lease_token": accepted["lease_token"],
            "registration": {
                "operation": "device.registration",
                "request_id": "request-register",
                "identity": {
                    "device_id": "device-1",
                    "public_key_fingerprint": "p256:functional-fingerprint",
                    "tenant_id": "local",
                },
                "manifest": {
                    "schema_version": 1,
                    "title": "Generic Device",
                    "properties": [],
                    "actions": [],
                    "events": [],
                    "media": [],
                },
                "display_name": "Generic Device",
                "device_kind": "generic",
            },
        },
    )
    assert registration.status_code == 200, registration.text
    assert registration.json()["device_id"] == "device-1"

    heartbeat = client.post(
        "/api/connection/v1/heartbeat",
        json={
            "operation": "connection.heartbeat",
            "request_id": "request-heartbeat",
            "connection_id": accepted["connection_id"],
            "lease_token": accepted["lease_token"],
            "sequence": 1,
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["registration_required"] is False
    duplicate = client.post(
        "/api/connection/v1/heartbeat",
        json={
            "operation": "connection.heartbeat",
            "request_id": "request-heartbeat-retry",
            "connection_id": accepted["connection_id"],
            "lease_token": accepted["lease_token"],
            "sequence": 1,
        },
    )
    assert duplicate.status_code == 200
    assert "device-1" in devices.values
    assert len(events.values) == 2


def test_https_binding_rejects_a_spoofed_connector_id() -> None:
    client, _devices, _connections, _events = _client()

    response = client.post(
        "/api/connection/v1/hello",
        json={
            "operation": "connection.hello",
            "request_id": "request-hello",
            "device_id": "device-1",
            "connector_id": "mqtt-cloud",
            "client_nonce": "0123456789abcdef",
        },
    )

    assert response.status_code == 422
