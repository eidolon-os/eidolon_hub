from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from hub.adapters.channels.data_bridge import ProviderDataChannelBridge
from hub.contracts.bindings.channel import DataEnvelope
from hub.domain.channels.entities import ChannelDataEnvelope, ChannelLease, ReportedStateData
from hub.domain.commands.entities import CommandState, DeviceCommand

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Sender:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)


class _Channels:
    def __init__(self):
        self.lease = ChannelLease(
            channel_id="channel-1",
            device_id="device-1",
            profile_name="management-data",
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )

    async def active_for_device(self, device_id, *, now, profile_name=None):
        if device_id == "device-1" and profile_name == "management-data":
            return (self.lease,)
        return ()


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


class _Ingest:
    def __init__(self):
        self.items = []

    async def execute(self, envelope):
        self.items.append(envelope)


def _command(device_id="device-1"):
    return DeviceCommand(
        command_id="command-1",
        device_id=device_id,
        operation="sensor.calibrate",
        payload_json='{"offset":1}',
        state=CommandState.QUEUED,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        updated_at=NOW,
    )


def _bridge():
    sender, cursors, ingest = _Sender(), _Cursors(), _Ingest()
    bridge = ProviderDataChannelBridge(
        sender=sender,
        channels=_Channels(),
        cursors=cursors,
        ingest=ingest,
        clock=_Clock(),
    )
    return bridge, sender, cursors, ingest


async def test_command_is_sent_as_provider_neutral_envelope() -> None:
    bridge, sender, _cursors, _ingest = _bridge()

    await bridge.send_command(_command())

    raw = sender.payloads[0]
    envelope = DataEnvelope.model_validate_json(raw)
    payload = json.loads(envelope.payload_json)
    assert envelope.kind == "command"
    assert envelope.sequence == 1
    assert payload["operation"] == "sensor.calibrate"
    assert "livekit" not in raw.decode().lower()


async def test_inbound_envelope_is_validated_and_deduplicated() -> None:
    bridge, _sender, _cursors, ingest = _bridge()
    envelope = DataEnvelope(
        envelope_id="envelope-1",
        channel_id="channel-1",
        device_id="device-1",
        kind="state",
        sequence=1,
        occurred_at_ms=int(NOW.timestamp() * 1000),
        payload_json='{"revision":1,"values_json":"{}"}',
    )

    await bridge.ingest_raw(envelope.model_dump_json().encode())
    await bridge.ingest_raw(envelope.model_dump_json().encode())

    assert ingest.items == [
        ChannelDataEnvelope(
            envelope_id="envelope-1",
            channel_id="channel-1",
            device_id="device-1",
            sequence=1,
            occurred_at=NOW,
            payload=ReportedStateData(revision=1, values_json="{}"),
        )
    ]


async def test_command_requires_active_management_channel() -> None:
    bridge, _sender, _cursors, _ingest = _bridge()

    with pytest.raises(ConnectionError, match="reliable data channel"):
        await bridge.send_command(_command(device_id="offline-device"))
