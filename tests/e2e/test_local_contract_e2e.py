from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jose import jwt
from werkzeug.wrappers import Response

from hub.composition.app import create_composed_app
from hub.config import ChannelProviderConfig, HubConfig, PersistenceConfig
from hub.contracts.bindings.channel import (
    CommandAckPayload,
    CommandResultPayload,
    DataEnvelope,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _admin_credential(secret: str) -> str:
    token = jwt.encode(
        {
            "sub": "e2e-admin",
            "aud": "eidolon-hub",
            "roles": ["hub-admin"],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )
    return f"Bearer {token}"


def _device_registration(fingerprint: str) -> dict[str, object]:
    return {
        "operation": "device.registration",
        "request_id": "e2e-register-1",
        "identity": {
            "device_id": "e2e-device-1",
            "public_key_fingerprint": fingerprint,
            "tenant_id": "local-e2e",
        },
        "manifest": {
            "schema_version": 1,
            "title": "E2E Device",
            "properties": [],
            "actions": [
                {
                    "name": "display.render",
                    "version": 1,
                    "input_schema": {},
                    "output_schema": {},
                    "idempotent": True,
                }
            ],
            "events": [],
            "media": [],
        },
        "display_name": "E2E Device",
        "device_kind": "reference-device",
    }


def test_local_black_box_contract_survives_hub_restart(tmp_path, monkeypatch, httpserver) -> None:
    lease_secret = "local-e2e-lease-secret-value-0001"
    management_secret = "local-e2e-management-secret-0001"
    provider_token = "local-e2e-provider-secret-value-1"
    monkeypatch.setenv("EIDOLON_HUB_LEASE_SECRET", lease_secret)
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", management_secret)
    monkeypatch.setenv("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", provider_token)
    received_provider_envelopes: list[dict[str, object]] = []

    def provider_acquire(request):
        body = request.get_json()
        now = datetime.now(UTC)
        return Response(
            json.dumps(
                {
                    "operation": "channel.assignments",
                    "operation_id": body["operation_id"],
                    "device_id": body["device"]["device_id"],
                    "manifest_revision": body["device"]["manifest_revision"],
                    "channels": [
                        {
                            "channel_id": "e2e-channel-1",
                            "purpose": "management",
                            "kinds": ["reliable-data"],
                            "binding_format": "application/reference-provider+json",
                            "issued_at_ms": int(now.timestamp() * 1000),
                            "expires_at_ms": int((now + timedelta(minutes=5)).timestamp() * 1000),
                            "opaque_binding": base64.b64encode(
                                b'{"url":"wss://provider.invalid","credential":"opaque"}'
                            ).decode(),
                        }
                    ],
                }
            ),
            content_type="application/json",
        )

    def provider_data(request):
        received_provider_envelopes.append(request.get_json())
        return Response('{"accepted":true}', content_type="application/json")

    httpserver.expect_request(
        "/v1/device-channels/acquire",
        method="POST",
        headers={"Authorization": f"Bearer {provider_token}"},
    ).respond_with_handler(provider_acquire)
    httpserver.expect_request(
        "/v1/data/envelopes",
        method="POST",
        headers={"Authorization": f"Bearer {provider_token}"},
    ).respond_with_handler(provider_data)

    config = replace(
        HubConfig(),
        observability=replace(HubConfig().observability, enabled=False),
        device_access=replace(
            HubConfig().device_access,
            public_base_url="https://hub.e2e.invalid",
        ),
        discovery=replace(
            HubConfig().discovery,
            mdns=replace(HubConfig().discovery.mdns, enabled=False),
        ),
        channel_provider=ChannelProviderConfig(contract_url=httpserver.url_for("/v1")),
        persistence=PersistenceConfig(
            adapter="sqlite",
            sqlite_path=str(tmp_path / "hub-e2e.sqlite3"),
            directory_cache_enabled=True,
            reconciliation_seconds=60,
        ),
    )
    admin = _admin_credential(management_secret)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key = _b64url(public_der)
    fingerprint = "p256:" + hashlib.sha256(public_der).hexdigest()

    with TestClient(create_composed_app(config)) as client:
        hello = client.post(
            "/api/device-access/v1/hello",
            json={
                "operation": "session.hello",
                "request_id": "e2e-hello-1",
                "device_id": "e2e-device-1",
                "client_nonce": "e2e-client-nonce-0001",
            },
        )
        assert hello.status_code == 200, hello.text
        challenge = hello.json()
        proof_bytes = "\n".join(
            (
                "eidolon.session.proof.v1",
                challenge["challenge_id"],
                "e2e-device-1",
                "e2e-client-nonce-0001",
                challenge["server_nonce"],
            )
        ).encode()
        signature = _b64url(private_key.sign(proof_bytes, ec.ECDSA(hashes.SHA256())))
        proof = client.post(
            "/api/device-access/v1/proof",
            json={
                "operation": "session.proof",
                "request_id": "e2e-proof-1",
                "challenge_id": challenge["challenge_id"],
                "device_id": "e2e-device-1",
                "public_key": public_key,
                "signature": signature,
            },
        )
        assert proof.status_code == 200, proof.text
        accepted = proof.json()
        registration = client.post(
            "/api/device-access/v1/register",
            json={
                "operation": "session.registration",
                "session_id": accepted["session_id"],
                "lease_token": accepted["lease_token"],
                "registration": _device_registration(fingerprint),
            },
        )
        assert registration.status_code == 200, registration.text
        approval = client.post(
            "/api/device-management/v1/devices/e2e-device-1/approval",
            headers={"Authorization": admin},
            json={
                "operation": "device.approval",
                "request_id": "e2e-approval-1",
                "owner_id": "e2e-owner-1",
            },
        )
        assert approval.status_code == 200, approval.text

        acquired = client.post(
            "/api/device-access/v1/channels/acquire",
            json={
                "operation": "channel.acquire",
                "request_id": "e2e-acquire-1",
                "session_id": accepted["session_id"],
                "lease_token": accepted["lease_token"],
            },
        )
        assert acquired.status_code == 200, acquired.text
        grant = acquired.json()["channels"][0]
        assert grant["channel_id"] == "e2e-channel-1"
        assert base64.b64decode(grant["opaque_binding"]).endswith(b'"opaque"}')

        lifecycle = client.post(
            "/api/provider/v1/channels/lifecycle",
            headers={"Authorization": f"Bearer {provider_token}"},
            json={
                "operation": "channel.lifecycle",
                "channel_id": grant["channel_id"],
                "device_id": "e2e-device-1",
                "state": "active",
                "occurred_at_ms": int(datetime.now(UTC).timestamp() * 1000),
                "reason": "",
            },
        )
        assert lifecycle.status_code == 200, lifecycle.text
        command = client.post(
            "/api/device-management/v1/devices/e2e-device-1/commands",
            headers={"Authorization": admin},
            json={
                "operation": "device.command",
                "request_id": "e2e-command-1",
                "command_name": "display.render",
                "arguments_json": '{"text":"hello"}',
                "ttl_ms": 30000,
            },
        )
        assert command.status_code == 200, command.text
        command_id = command.json()["command_id"]
        assert received_provider_envelopes[0]["kind"] == "command"

        for sequence, kind, payload in (
            (
                1,
                "ack",
                CommandAckPayload(command_id=command_id, status="accepted").model_dump_json(),
            ),
            (
                2,
                "result",
                CommandResultPayload(
                    command_id=command_id,
                    status="succeeded",
                    result_json='{"ok":true}',
                ).model_dump_json(),
            ),
        ):
            inbound = client.post(
                "/api/provider/v1/data/inbound",
                headers={"Authorization": f"Bearer {provider_token}"},
                content=DataEnvelope(
                    envelope_id=f"e2e-envelope-{sequence}",
                    channel_id=grant["channel_id"],
                    device_id="e2e-device-1",
                    kind=kind,
                    sequence=sequence,
                    occurred_at_ms=int(datetime.now(UTC).timestamp() * 1000),
                    payload_json=payload,
                ).model_dump_json(),
            )
            assert inbound.status_code == 200, inbound.text
        status = client.get(
            f"/api/device-management/v1/commands/{command_id}",
            headers={"Authorization": admin},
        )
        assert status.json()["state"] == "succeeded"
        directory = client.get(
            "/api/device-management/v1/directory/e2e-owner-1",
            headers={"Authorization": admin},
        )
        assert directory.json()[0]["manifest_revision"] == registration.json()["manifest_revision"]

    with TestClient(create_composed_app(config)) as restarted:
        directory_after_restart = restarted.get(
            "/api/device-management/v1/directory/e2e-owner-1",
            headers={"Authorization": admin},
        )
        assert directory_after_restart.status_code == 200
        assert directory_after_restart.json()[0]["device_id"] == "e2e-device-1"
