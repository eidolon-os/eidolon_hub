from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI

from hub.adapters.channels.data_bridge import HttpDataEnvelopeSender, ProviderDataChannelBridge
from hub.adapters.channels.provider_client import (
    ChannelProviderHttpClient,
    HttpRequestReplyClient,
)
from hub.application.use_cases.ingest_data_envelope import IngestDataEnvelope
from hub.application.use_cases.reconcile_device_channels import ReconcileDeviceChannels
from hub.application.use_cases.record_channel_lifecycle import RecordChannelLifecycle
from hub.application.use_cases.send_command import SendCommand
from hub.contracts.bindings.channel import (
    CommandAckPayload,
    CommandResultPayload,
    DataEnvelope,
    ProviderChannelSyncRequest,
)
from hub.domain.channels.entities import (
    ChannelLifecycle,
    ChannelState,
    ProviderSyncState,
)
from hub.domain.commands.entities import CommandState
from hub.domain.connections.entities import ConnectionLease, ConnectorKind
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Ids:
    def new(self, prefix):
        return f"{prefix}-functional-1"


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1", "p256:fingerprint", "local"),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping(
                {
                    "schema_version": 1,
                    "title": "Device",
                    "properties": [],
                    "actions": [],
                    "events": [],
                    "media": [],
                }
            ),
            registered_at=NOW,
            updated_at=NOW,
            owner_id="owner-1",
            approved=True,
        )

    async def get(self, device_id):
        return self.device if device_id == "device-1" else None

    async def list_all(self):
        return (self.device,)


class _Connections:
    def __init__(self):
        self.lease = ConnectionLease(
            connection_id="connection-1",
            device_id="device-1",
            connector_id="https-local",
            connector_kind=ConnectorKind.HTTPS,
            signaling_ref="mailbox:device-1",
            opened_at=NOW,
            renewed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            lease_token="lease-token-device-1",
            identity_fingerprint="p256:fingerprint",
            hub_instance_id="hub-local-1",
            fencing_token=1,
        )

    async def active_for_device(self, device_id, *, now):
        return (self.lease,) if device_id == "device-1" and self.lease.is_active(now) else ()


class _Syncs:
    def __init__(self):
        self.value = None

    async def get(self, device_id):
        return self.value

    async def try_claim(self, desired, *, owner_instance_id, now, claim_ttl):
        self.value = replace(
            desired,
            state=ProviderSyncState.SYNCHRONIZING,
            attempts=(self.value.attempts + 1 if self.value else 1),
            owner_instance_id=owner_instance_id,
            claim_expires_at=now + claim_ttl,
        )
        return self.value

    async def mark_succeeded(self, *, device_id, operation_id, now):
        self.value = replace(
            self.value,
            state=ProviderSyncState.SUCCEEDED,
            owner_instance_id="",
            claim_expires_at=None,
            updated_at=now,
        )

    async def mark_failed(self, *, device_id, operation_id, now, error):
        self.value = replace(
            self.value,
            state=ProviderSyncState.FAILED,
            owner_instance_id="",
            claim_expires_at=None,
            updated_at=now,
            last_error=error,
        )


class _Channels:
    def __init__(self):
        self.values = {}

    async def get(self, channel_id):
        return self.values.get(channel_id)

    async def upsert(self, lease):
        self.values[lease.channel_id] = lease
        return lease

    async def delete(self, channel_id):
        self.values.pop(channel_id, None)

    async def list_for_device(self, device_id):
        return tuple(value for value in self.values.values() if value.device_id == device_id)

    async def active_for_device(self, device_id, *, now, purpose=None):
        return tuple(
            value
            for value in self.values.values()
            if value.device_id == device_id
            and value.is_active(now)
            and (purpose is None or value.purpose == purpose)
        )


class _Commands:
    def __init__(self):
        self.values = {}

    async def get(self, command_id):
        return self.values.get(command_id)

    async def upsert(self, command):
        self.values[command.command_id] = command
        return command


class _Events:
    def __init__(self):
        self.values = []

    async def publish(self, event):
        self.values.append(event)


class _Cursors:
    def __init__(self):
        self.outbound = 0
        self.inbound = set()

    async def next_outbound(self, channel_id):
        self.outbound += 1
        return self.outbound

    async def accept_inbound(self, *, channel_id, sequence, envelope_id):
        key = channel_id, sequence, envelope_id
        if key in self.inbound:
            return False
        self.inbound.add(key)
        return True


class _Signaling:
    def __init__(self):
        self.grants = []

    async def send_grant(self, *, signaling_ref, grant):
        self.grants.append((signaling_ref, grant))


def _provider_app(received_envelopes):
    app = FastAPI()

    @app.post("/v1/device-channels/sync")
    async def sync_device(request: ProviderChannelSyncRequest):
        channels = []
        if request.device.approved and request.device.connected and not request.device.revoked:
            channels.append(
                {
                    "channel_id": "channel-1",
                    "purpose": "management",
                    "kinds": ["reliable-data"],
                    "binding_format": "application/reference-provider+json",
                    "issued_at_ms": int(NOW.timestamp() * 1000),
                    "expires_at_ms": int((NOW + timedelta(minutes=5)).timestamp() * 1000),
                    "opaque_binding": base64.b64encode(
                        b'{"url":"wss://provider.test","credential":"provider-secret"}'
                    ).decode(),
                }
            )
        return {
            "operation": "channel.assignments",
            "operation_id": request.operation_id,
            "device_id": request.device.device_id,
            "manifest_revision": request.device.manifest_revision,
            "channels": channels,
        }

    @app.post("/v1/data/envelopes")
    async def receive_envelope(envelope: DataEnvelope):
        received_envelopes.append(envelope)
        return {"accepted": True}

    return app


async def test_provider_http_contract_to_active_command_ack_result_round_trip() -> None:
    provider_envelopes = []
    transport = httpx.ASGITransport(app=_provider_app(provider_envelopes))
    async with httpx.AsyncClient(transport=transport, base_url="http://provider.test") as client:
        provider = ChannelProviderHttpClient(
            HttpRequestReplyClient(client), contract_url="http://provider.test/v1"
        )
        devices, connections, channels = _Devices(), _Connections(), _Channels()
        syncs, signaling = _Syncs(), _Signaling()
        result = await ReconcileDeviceChannels(
            hub_id="hub-local",
            hub_instance_id="hub-local-1",
            devices=devices,
            connections=connections,
            syncs=syncs,
            channel_leases=channels,
            provider=provider,
            grant_sender=signaling,
            clock=_Clock(),
        ).execute()
        assert result.succeeded == 1
        assert channels.values["channel-1"].state is ChannelState.PENDING
        assert signaling.grants[0][1].opaque_binding.relay_bytes().endswith(b'"provider-secret"}')

        await RecordChannelLifecycle(leases=channels).execute(
            ChannelLifecycle("channel-1", "device-1", ChannelState.ACTIVE, NOW)
        )
        commands, events = _Commands(), _Events()
        ingest = IngestDataEnvelope(
            channels=channels, commands=commands, events=events, clock=_Clock()
        )
        bridge = ProviderDataChannelBridge(
            sender=HttpDataEnvelopeSender(client, route="http://provider.test/v1/data/envelopes"),
            channels=channels,
            cursors=_Cursors(),
            ingest=ingest,
            clock=_Clock(),
        )
        sent = await SendCommand(
            devices=devices,
            commands=commands,
            sender=bridge,
            clock=_Clock(),
            ids=_Ids(),
        ).execute(device_id="device-1", operation="display.render", payload_json='{"text":"hi"}')
        assert sent.state is CommandState.SENT
        assert provider_envelopes[0].kind == "command"

        for sequence, kind, payload in (
            (
                1,
                "ack",
                CommandAckPayload(command_id=sent.command_id, status="accepted").model_dump_json(),
            ),
            (
                2,
                "result",
                CommandResultPayload(
                    command_id=sent.command_id,
                    status="succeeded",
                    result_json='{"rendered":true}',
                ).model_dump_json(),
            ),
        ):
            await bridge.ingest_raw(
                DataEnvelope(
                    envelope_id=f"device-envelope-{sequence}",
                    channel_id="channel-1",
                    device_id="device-1",
                    kind=kind,
                    sequence=sequence,
                    occurred_at_ms=int(NOW.timestamp() * 1000),
                    payload_json=payload,
                )
                .model_dump_json()
                .encode()
            )
        assert commands.values[sent.command_id].state is CommandState.SUCCEEDED
        assert commands.values[sent.command_id].result_json == '{"rendered":true}'


async def test_provider_binding_is_not_present_in_sync_state_or_channel_lease() -> None:
    provider_envelopes = []
    transport = httpx.ASGITransport(app=_provider_app(provider_envelopes))
    async with httpx.AsyncClient(transport=transport, base_url="http://provider.test") as client:
        channels, syncs, signaling = _Channels(), _Syncs(), _Signaling()
        await ReconcileDeviceChannels(
            hub_id="hub-local",
            hub_instance_id="hub-local-1",
            devices=_Devices(),
            connections=_Connections(),
            syncs=syncs,
            channel_leases=channels,
            provider=ChannelProviderHttpClient(
                HttpRequestReplyClient(client), contract_url="http://provider.test/v1"
            ),
            grant_sender=signaling,
            clock=_Clock(),
        ).execute()

    assert "provider-secret" not in repr(syncs.value)
    assert "provider-secret" not in repr(channels.values["channel-1"])
    assert "provider-secret" not in json.dumps(channels.values["channel-1"].channel_id)
