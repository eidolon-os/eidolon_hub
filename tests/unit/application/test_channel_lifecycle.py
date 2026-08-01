from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.record_channel_lifecycle import RecordChannelLifecycle
from hub.domain.channels.entities import (
    ChannelKind,
    ChannelLease,
    ChannelLifecycle,
    ChannelState,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Leases:
    def __init__(self):
        self.value = ChannelLease(
            channel_id="channel-1",
            device_id="device-1",
            purpose="management",
            kinds=frozenset({ChannelKind.RELIABLE_DATA}),
            binding_format="application/eidolon-test+json",
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
            state=ChannelState.PENDING,
        )

    async def get(self, channel_id):
        return (
            self.value if self.value is not None and channel_id == self.value.channel_id else None
        )

    async def upsert(self, lease):
        self.value = lease
        return lease


async def test_provider_active_event_is_the_only_activation_authority() -> None:
    leases = _Leases()

    await RecordChannelLifecycle(leases=leases).execute(
        ChannelLifecycle("channel-1", "device-1", ChannelState.ACTIVE, NOW)
    )

    assert leases.value.state is ChannelState.ACTIVE
    assert leases.value.updated_at == NOW


async def test_lifecycle_event_cannot_target_another_device() -> None:
    leases = _Leases()

    with pytest.raises(PermissionError, match="does not belong"):
        await RecordChannelLifecycle(leases=leases).execute(
            ChannelLifecycle("channel-1", "device-2", ChannelState.ACTIVE, NOW)
        )


async def test_closed_channel_is_terminal_and_duplicate_close_is_idempotent() -> None:
    leases = _Leases()
    use_case = RecordChannelLifecycle(leases=leases)
    closed = ChannelLifecycle("channel-1", "device-1", ChannelState.CLOSED, NOW)
    await use_case.execute(closed)
    await use_case.execute(closed)

    with pytest.raises(ValueError, match="invalid channel transition"):
        await use_case.execute(
            ChannelLifecycle(
                "channel-1", "device-1", ChannelState.ACTIVE, NOW + timedelta(seconds=1)
            )
        )


async def test_unknown_terminal_callback_is_idempotent_but_unknown_active_is_rejected() -> None:
    leases = _Leases()
    leases.value = None
    use_case = RecordChannelLifecycle(leases=leases)

    await use_case.execute(ChannelLifecycle("missing", "device-1", ChannelState.CLOSED, NOW))
    with pytest.raises(KeyError):
        await use_case.execute(ChannelLifecycle("missing", "device-1", ChannelState.ACTIVE, NOW))
