from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from jose import jwt
from werkzeug.wrappers import Response

from hub.composition.app import create_composed_app
from hub.config import (
    ChannelProviderConfig,
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    OnboardingConfig,
    PersistenceConfig,
)

TOKEN = "device-generated-random-token-000001"


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


def _registry_reader_credential(token: str) -> str:
    return f"Bearer {token}"


def _device_enrollment():
    return {
        "operation": "device.enrollment",
        "request_id": "e2e-enroll-1",
        "retrieval_token": TOKEN,
        "identity": {"device_id": "e2e-device-1"},
        "manifest": {
            "schema_version": 1,
            "title": "E2E Device",
            "actions": [
                {
                    "name": "display.render",
                    "version": 1,
                    "input_schema": {},
                    "output_schema": {},
                    "idempotent": True,
                }
            ],
        },
        "display_name": "E2E Device",
        "device_kind": "reference-device",
    }


def _handoff():
    return {
        "operation": "device.handoff",
        "request_id": "e2e-handoff-1",
        "retrieval_token": TOKEN,
    }


def test_local_enrollment_handoff_and_directory_survive_restart(
    tmp_path, monkeypatch, httpserver
) -> None:
    management_secret = "local-e2e-management-secret-0001"
    provider_token = "local-e2e-provider-secret-value-1"
    registry_reader_token = "local-e2e-registry-reader-token-0001"
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", management_secret)
    monkeypatch.setenv("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", provider_token)
    monkeypatch.setenv(
        "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN",
        registry_reader_token,
    )

    def provider_provision(request):
        body = request.get_json()
        now = datetime.now(UTC)
        return Response(
            json.dumps(
                {
                    "operation": "channel.provisioned-device",
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
                            "expires_at_ms": int(
                                (now + timedelta(minutes=5)).timestamp() * 1000
                            ),
                            "opaque_binding": base64.b64encode(
                                b'{"url":"wss://provider.invalid","credential":"opaque"}'
                            ).decode(),
                        }
                    ],
                }
            ),
            content_type="application/json",
        )

    httpserver.expect_request(
        "/v1/device-channels/provision",
        method="POST",
        headers={"Authorization": f"Bearer {provider_token}"},
    ).respond_with_handler(provider_provision)

    config = HubConfig(
        onboarding=OnboardingConfig(public_base_url="https://hub.e2e.invalid"),
        discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        channel_provider=ChannelProviderConfig(contract_url=httpserver.url_for("/v1")),
        persistence=PersistenceConfig(path=str(tmp_path / "hub-e2e.sqlite3")),
    )
    admin = _admin_credential(management_secret)
    registry_reader = _registry_reader_credential(registry_reader_token)

    with TestClient(create_composed_app(config)) as client:
        receipt = client.post(
            "/api/device-onboarding/v1/enrollments", json=_device_enrollment()
        )
        assert receipt.status_code == 200, receipt.text
        enrollment_id = receipt.json()["enrollment_id"]
        pending = client.post(
            f"/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff",
            json=_handoff(),
        )
        assert pending.status_code == 202

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
        handed_off = client.post(
            f"/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff",
            json=_handoff(),
        )
        assert handed_off.status_code == 200, handed_off.text
        assert handed_off.json()["lifecycle_state"] == "approved"
        assert base64.b64decode(
            handed_off.json()["channels"][0]["opaque_binding"]
        ).endswith(b'"opaque"}')

        detail = client.get(
            "/api/device-management/v1/owners/e2e-owner-1/devices/e2e-device-1",
            headers={"Authorization": admin},
        )
        assert detail.status_code == 200
        assert detail.json()["manifest"]["title"] == "E2E Device"
        assert "online" not in detail.json()

        kernel_detail = client.get(
            "/api/device-management/v1/owners/e2e-owner-1/devices/e2e-device-1",
            headers={"Authorization": registry_reader},
        )
        assert kernel_detail.status_code == 200
        assert kernel_detail.json()["lifecycle_state"] == "approved"

        forbidden_requests = (
            client.get(
                "/api/device-management/v1/owners/e2e-owner-1/devices",
                headers={"Authorization": registry_reader},
            ),
            client.get(
                "/api/device-management/v1/owners/e2e-owner-1/events",
                headers={"Authorization": registry_reader},
            ),
            client.post(
                "/api/device-management/v1/devices/e2e-device-1/approval",
                headers={"Authorization": registry_reader},
                json={
                    "operation": "device.approval",
                    "request_id": "e2e-reader-approval-1",
                    "owner_id": "e2e-owner-1",
                },
            ),
            client.post(
                "/api/device-management/v1/devices/e2e-device-1/revocation",
                headers={"Authorization": registry_reader},
                json={
                    "operation": "device.revocation",
                    "request_id": "e2e-reader-revocation-1",
                    "reason": "must-not-run",
                },
            ),
        )
        assert all(response.status_code == 403 for response in forbidden_requests)

    with TestClient(create_composed_app(config)) as restarted:
        directory = restarted.get(
            "/api/device-management/v1/owners/e2e-owner-1/devices",
            headers={"Authorization": admin},
        )
        assert directory.status_code == 200
        assert directory.json()["devices"][0]["device_id"] == "e2e-device-1"
