from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from jsonschema import Draft202012Validator

from hub.adapters.channels.provider_client import ChannelProviderHttpClient
from hub.domain.channels.entities import ProviderDeviceContext

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Transport:
    def __init__(self):
        self.calls = []

    async def request(self, route, payload, *, timeout):
        request = json.loads(payload)
        self.calls.append((route, request, timeout))
        return json.dumps(
            {
                "operation": "channel.assignments",
                "operation_id": request["operation_id"],
                "device_id": request["device"]["device_id"],
                "manifest_revision": request["device"]["manifest_revision"],
                "channels": [
                    {
                        "channel_id": "channel-1",
                        "purpose": "management",
                        "kinds": ["reliable-data"],
                        "binding_format": "application/eidolon-test+json",
                        "issued_at_ms": int(NOW.timestamp() * 1000),
                        "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                        "opaque_binding": base64.b64encode(b"provider-owned").decode(),
                    }
                ],
            }
        ).encode()


async def test_provider_client_uses_one_fixed_acquisition_contract_and_preserves_binding() -> None:
    transport = _Transport()
    client = ChannelProviderHttpClient(
        transport,
        contract_url="https://provider.example/v1",
        timeout_seconds=3,
    )
    context = ProviderDeviceContext(
        operation_id="acquire-1",
        hub_id="hub-1",
        device_id="device-1",
        public_key_fingerprint="p256:fingerprint",
        tenant_id="default",
        owner_id="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json='{"schema_version":1,"title":"Device"}',
        manifest_revision="sha256:manifest",
        approved=True,
        revoked=False,
        connected=True,
    )

    assignments = await client.acquire_channels(context)

    assert transport.calls[0][0] == "https://provider.example/v1/device-channels/acquire"
    assert transport.calls[0][1]["operation"] == "channel.acquire-device"
    assert transport.calls[0][1]["device"]["public_key_fingerprint"] == "p256:fingerprint"
    assert transport.calls[0][1]["device"]["owner_id"] == "owner-1"
    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned"


def test_provider_acquisition_schema_has_strict_request_and_response_defs() -> None:
    schema = json.loads(
        open(
            "hub/contracts/schemas/channel/provider-acquisition.schema.json", encoding="utf-8"
        ).read()
    )
    Draft202012Validator.check_schema(schema)
    assert schema["$defs"]["request"]["additionalProperties"] is False
    assert schema["$defs"]["response"]["additionalProperties"] is False
