from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.handle_channel_signal import HandleChannelSignal
from hub.domain.channels.entities import (
    ChannelLease,
    ChannelNegotiationIntent,
    ChannelNegotiationOperation,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Channels:
    def __init__(self):
        self.lease = ChannelLease(
            channel_id="channel-1",
            device_id="device-1",
            profile_name="management-data",
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=1),
        )

    async def get(self, channel_id):
        return self.lease if channel_id == "channel-1" else None


class _Revoke:
    def __init__(self):
        self.values = []

    async def execute(self, channel_id, *, reason):
        self.values.append((channel_id, reason))


class _Events:
    def __init__(self):
        self.values = []

    async def publish(self, event):
        self.values.append(event)


def _signal(operation, *, device_id="device-1"):
    return ChannelNegotiationIntent(
        operation=operation,
        request_id=f"request-{operation.value}",
        device_id=device_id,
        connection_id="connection-1",
        channel_id="channel-1",
    )


async def test_accept_records_lifecycle_and_close_revokes_provider_lease() -> None:
    channels, revoke, events = _Channels(), _Revoke(), _Events()
    use_case = HandleChannelSignal(
        channels=channels,
        revoke_channel=revoke,
        events=events,
        clock=_Clock(),
    )

    await use_case.execute(_signal(ChannelNegotiationOperation.ACCEPT))
    await use_case.execute(_signal(ChannelNegotiationOperation.CLOSE))

    assert len(events.values) == 2
    assert revoke.values == [("channel-1", "device-closed")]


async def test_signal_cannot_claim_another_devices_channel() -> None:
    use_case = HandleChannelSignal(
        channels=_Channels(),
        revoke_channel=_Revoke(),
        events=_Events(),
        clock=_Clock(),
    )

    with pytest.raises(PermissionError, match="does not belong"):
        await use_case.execute(_signal(ChannelNegotiationOperation.ACCEPT, device_id="device-2"))


async def test_offer_without_channel_is_recorded_and_expired_channel_is_rejected() -> None:
    channels, events = _Channels(), _Events()
    use_case = HandleChannelSignal(
        channels=channels,
        revoke_channel=_Revoke(),
        events=events,
        clock=_Clock(),
    )
    await use_case.execute(
        ChannelNegotiationIntent(
            operation=ChannelNegotiationOperation.OFFER,
            request_id="request-offer",
            device_id="device-1",
            connection_id="connection-1",
        )
    )
    assert events.values[0].event_type == "eidolon.channel.offer.v1"

    channels.lease = ChannelLease(
        channel_id="channel-1",
        device_id="device-1",
        profile_name="management-data",
        issued_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(PermissionError, match="expired"):
        await use_case.execute(_signal(ChannelNegotiationOperation.ACCEPT))
