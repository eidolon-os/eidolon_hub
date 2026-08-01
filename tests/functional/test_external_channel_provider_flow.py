from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from hub.adapters.channels.data_bridge import ProviderDataChannelBridge
from hub.adapters.channels.grant_sender import GrantSignalingRouter, MqttSignalingTransport
from hub.application.use_cases.ingest_data_envelope import IngestDataEnvelope
from hub.application.use_cases.provision_channel import ProvisionChannel
from hub.application.use_cases.send_command import SendCommand
from hub.contracts.bindings.channel import CommandAckPayload, CommandResultPayload, DataEnvelope
from hub.domain.channels.entities import (
    ChannelGrant,
    ChannelKind,
    ChannelLease,
    ChannelProfile,
    OpaqueChannelBinding,
)
from hub.domain.channels.selection import ChannelProfileCatalog
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
            identity=DeviceIdentity("device-1", "p256:fingerprint"),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping({"schema_version": 1}),
            registered_at=NOW,
            updated_at=NOW,
            owner_id="owner-1",
            approved=True,
        )

    async def get(self, device_id):
        return self.device if device_id == self.device.identity.device_id else None


class _Connections:
    def __init__(self, kind=ConnectorKind.HTTPS, signaling_ref="http-mailbox:device-1"):
        self.lease = ConnectionLease(
            connection_id="connection-1",
            device_id="device-1",
            connector_id="connector-1",
            connector_kind=kind,
            signaling_ref=signaling_ref,
            opened_at=NOW,
            renewed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            lease_token="lease-token-device-1",
            identity_fingerprint="p256:fingerprint",
            hub_instance_id="hub-1",
            fencing_token=1,
        )

    async def active_for_device(self, device_id, *, now):
        return (
            (self.lease,) if self.lease.device_id == device_id and self.lease.is_active(now) else ()
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

    async def active_for_device(self, device_id, *, now, profile_name=None):
        return tuple(
            value
            for value in self.values.values()
            if value.device_id == device_id
            and value.expires_at > now
            and (profile_name is None or value.profile_name == profile_name)
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


class _DataSender:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)


class _Provider:
    def __init__(self, opaque_binding):
        self.opaque_binding = opaque_binding

    async def provision(self, request):
        return ChannelGrant(
            request_id=request.request_id,
            lease=ChannelLease(
                channel_id="channel-1",
                device_id=request.device_id,
                profile_name=request.profile.name,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            ),
            opaque_binding=OpaqueChannelBinding(self.opaque_binding),
        )


class _Signaling:
    def __init__(self):
        self.grant = None

    async def send_grant(self, *, signaling_ref, grant):
        self.grant = grant


class _MqttWriter:
    def __init__(self):
        self.payload = None

    async def send_signal(self, *, device_id, payload):
        self.device_id = device_id
        self.payload = payload


def _profile(name="management-data", kinds=frozenset({ChannelKind.RELIABLE_DATA})):
    return ChannelProfile(name, kinds, f"provider/{name}")


async def test_wss_only_provider_completes_command_ack_and_result_round_trip() -> None:
    """The provider owns WSS; Hub sees only its binding and DataEnvelope bridge."""
    devices, connections, channels = _Devices(), _Connections(), _Channels()
    commands, events, data_sender, signaling = _Commands(), _Events(), _DataSender(), _Signaling()
    profile = _profile()
    provider_binding = b'{"url":"wss://provider.example/device","credential":"provider-secret"}'
    grant = await ProvisionChannel(
        profiles=ChannelProfileCatalog((profile,)),
        provisioners={profile.provisioner_ref: _Provider(provider_binding)},
        grant_sender=signaling,
        devices=devices,
        connections=connections,
        channel_leases=channels,
        clock=_Clock(),
        ids=_Ids(),
    ).execute(device_id="device-1", profile_name=profile.name)
    assert signaling.grant is grant
    assert grant.opaque_binding.relay_bytes() == provider_binding

    ingest = IngestDataEnvelope(
        channels=channels,
        commands=commands,
        events=events,
        clock=_Clock(),
    )
    bridge = ProviderDataChannelBridge(
        sender=data_sender,
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
    outbound = DataEnvelope.model_validate_json(data_sender.payloads[0])
    assert sent.state is CommandState.SENT
    assert outbound.kind == "command"

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
                channel_id=grant.lease.channel_id,
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


async def test_wan_mqtt_relays_an_external_realtime_provider_binding_unchanged() -> None:
    """An external LiveKit-like provider is only opaque bytes to Hub."""
    connections = _Connections(ConnectorKind.MQTT5, "mqtt:device-1")
    channels, writer = _Channels(), _MqttWriter()
    profile = _profile(
        "realtime-media",
        frozenset({ChannelKind.REALTIME_DATA, ChannelKind.AUDIO, ChannelKind.VIDEO}),
    )
    provider_binding = json.dumps(
        {
            "provider": "livekit",
            "url": "wss://external.example",
            "room": "provider-room",
            "token": "provider-token",
        }
    ).encode()
    await ProvisionChannel(
        profiles=ChannelProfileCatalog((profile,)),
        provisioners={profile.provisioner_ref: _Provider(provider_binding)},
        grant_sender=GrantSignalingRouter({"mqtt": MqttSignalingTransport(writer)}),
        devices=_Devices(),
        connections=connections,
        channel_leases=channels,
        clock=_Clock(),
        ids=_Ids(),
    ).execute(device_id="device-1", profile_name=profile.name)

    signal = json.loads(writer.payload)
    assert writer.device_id == "device-1"
    assert base64.b64decode(signal["opaque_binding"]) == provider_binding
    assert "provider-token" not in writer.payload.decode()
    assert channels.values["channel-1"].profile_name == "realtime-media"
