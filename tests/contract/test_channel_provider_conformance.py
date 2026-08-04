from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from hub.adapters.channels.provider_client import ChannelProviderHttpClient
from hub.domain.channels.entities import ProviderDeviceContext

NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _ProviderTransport:
    async def request(self, route, payload, *, timeout):
        request = json.loads(payload)
        assert request["operation"] == "channel.provision-device"
        return json.dumps(
            {
                "operation": "channel.provisioned-device",
                "operation_id": request["operation_id"],
                "device_id": request["device"]["device_id"],
                "manifest_revision": request["device"]["manifest_revision"],
                "channels": [
                    {
                        "channel_id": "channel-1",
                        "purpose": "management",
                        "kinds": ["reliable-data"],
                        "binding_format": "application/test+json",
                        "issued_at_ms": int(NOW.timestamp() * 1000),
                        "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                        "opaque_binding": base64.b64encode(b"provider-owned").decode(),
                    }
                ],
            }
        ).encode()


async def test_channel_provider_adapter_conforms_to_provision_port() -> None:
    client = ChannelProviderHttpClient(
        _ProviderTransport(), contract_url="https://provider.example/v1"
    )
    context = ProviderDeviceContext(
        operation_id="enrollment-1",
        hub_id="hub-1",
        device_id="device-1",
        owner_id="owner-1",
        display_name="Device",
        device_kind="generic",
        manifest_json='{"schema_version":1,"title":"Device"}',
        manifest_revision="sha256:manifest",
    )

    assignments = await client.provision_channels(context)

    assert assignments.device_id == "device-1"
    assert assignments.grants[0].opaque_binding.relay_bytes() == b"provider-owned"
