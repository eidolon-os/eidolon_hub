from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from jsonschema import Draft202012Validator

from hub.adapters.channels.provider_client import ChannelProviderHttpClient
from hub.domain.channels.entities import ProviderChannelRevocation, ProviderDeviceContext

NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _Transport:
    def __init__(self):
        self.calls = []

    async def request(self, route, payload, *, timeout):
        request = json.loads(payload)
        self.calls.append((route, request, timeout))
        if route.endswith("/revoke"):
            return json.dumps(
                {
                    "operation": "channel.revoked-device",
                    "operation_id": request["operation_id"],
                    "device_id": request["device_id"],
                }
            ).encode()
        return json.dumps(
            {
                "operation": "channel.provisioned-device",
                "operation_id": request["operation_id"],
                "device_id": request["device"]["device_id"],
                "manifest_revision": request["device"]["manifest_revision"],
                "channels": [
                    {
                        "channel_id": "channel-1",
                        "purpose": "provider-selected",
                        "kinds": ["reliable-data"],
                        "binding_format": "application/eidolon-test+json",
                        "issued_at_ms": int(NOW.timestamp() * 1000),
                        "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                        "opaque_binding": base64.b64encode(b"provider-owned").decode(),
                    }
                ],
            }
        ).encode()


def _context():
    return ProviderDeviceContext(
        operation_id="enrollment-1",
        hub_id="hub-1",
        device_id="device-1",
        owner_id="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json='{"schema_version":1,"title":"Device"}',
        manifest_revision="sha256:manifest",
    )


async def test_provider_client_uses_fixed_provision_contract_and_preserves_binding() -> None:
    transport = _Transport()
    client = ChannelProviderHttpClient(
        transport, contract_url="https://provider.example/v1", timeout_seconds=3
    )

    assignments = await client.provision_channels(_context())

    assert transport.calls[0][0] == "https://provider.example/v1/device-channels/provision"
    assert transport.calls[0][1]["operation"] == "channel.provision-device"
    assert transport.calls[0][1]["device"]["owner_id"] == "owner-1"
    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned"


async def test_provider_client_uses_separate_idempotent_revoke_contract() -> None:
    transport = _Transport()
    client = ChannelProviderHttpClient(transport, contract_url="https://provider.example/v1")

    await client.revoke_channels(
        ProviderChannelRevocation("revoke-1", "hub-1", "device-1", "operator-request")
    )

    assert transport.calls[0][0] == "https://provider.example/v1/device-channels/revoke"
    assert transport.calls[0][1]["operation"] == "channel.revoke-device"


def test_provider_control_schema_has_strict_provision_and_revoke_defs() -> None:
    schema = json.loads(
        open(
            "hub/contracts/schemas/channel/provider-provision.schema.json", encoding="utf-8"
        ).read()
    )
    Draft202012Validator.check_schema(schema)
    for name in ("provisionRequest", "provisionResponse", "revokeRequest", "revokeResponse"):
        assert schema["$defs"][name]["additionalProperties"] is False
