from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI

from hub.adapters.channels.provider_client import ChannelProviderHttpClient, HttpRequestReplyClient
from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.contracts.bindings.channel import (
    ProviderChannelProvisionRequest,
    ProviderChannelRevocationRequest,
)
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1"),
            enrollment_id="enrollment-1",
            retrieval_token_hash="a" * 64,
            retrieval_expires_at=NOW + timedelta(minutes=30),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1, "title": "Device"}),
            enrolled_at=NOW,
            updated_at=NOW,
            owner_id="owner-1",
            lifecycle_state=DeviceLifecycleState.APPROVED,
        )

    async def get(self, device_id):
        return self.device if device_id == self.device.identity.device_id else None

    async def commit(self, *, expected, device, event):
        assert self.device == expected
        self.device = device
        return device


class _Projector:
    async def execute(self, device_id):
        self.device_id = device_id


def _provider_app(provisions, revocations):
    app = FastAPI()

    @app.post("/v1/device-channels/provision")
    async def provision(request: ProviderChannelProvisionRequest):
        provisions.append(request)
        return {
            "operation": "channel.provisioned-device",
            "operation_id": request.operation_id,
            "device_id": request.device.device_id,
            "manifest_revision": request.device.manifest_revision,
            "channels": [
                {
                    "channel_id": "channel-1",
                    "purpose": "provider-selected",
                    "kinds": ["reliable-data"],
                    "binding_format": "application/reference-provider+json",
                    "issued_at_ms": int(NOW.timestamp() * 1000),
                    "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                    "opaque_binding": base64.b64encode(
                        b'{"url":"wss://provider.test","credential":"provider-secret"}'
                    ).decode(),
                }
            ],
        }

    @app.post("/v1/device-channels/revoke")
    async def revoke(request: ProviderChannelRevocationRequest):
        revocations.append(request)
        return {
            "operation": "channel.revoked-device",
            "operation_id": request.operation_id,
            "device_id": request.device_id,
        }

    return app


async def test_provision_relay_and_revoke_are_control_only() -> None:
    provisions, revocations = [], []
    transport = httpx.ASGITransport(app=_provider_app(provisions, revocations))
    async with httpx.AsyncClient(transport=transport, base_url="http://provider.test") as client:
        provider = ChannelProviderHttpClient(
            HttpRequestReplyClient(client), contract_url="http://provider.test/v1"
        )
        devices = _Devices()
        assignments = await ProvisionDeviceChannels(
            hub_id="hub-local",
            provider=provider,
            clock=_Clock(),
        ).execute(
            device=devices.device,
            operation_id="enrollment-1",
        )
        assert assignments.grants[0].opaque_binding.relay_bytes().endswith(b'"provider-secret"}')
        assert "provider-secret" not in repr(assignments)
        await RevokeDevice(
            devices=devices,
            provider=provider,
            hub_id="hub-local",
            mutations=devices,
            clock=_Clock(),
            directory_projector=_Projector(),
        ).execute(
            device_id="device-1",
            reason="operator-request",
            request_id="revoke-1",
            principal_id="owner-operator",
        )

    assert provisions[0].device.device_id == "device-1"
    assert revocations[0].device_id == "device-1"
