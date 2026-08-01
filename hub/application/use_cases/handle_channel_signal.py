"""Handle authenticated device channel lifecycle signaling."""

from __future__ import annotations

import json

from hub.application.use_cases.revoke_channel import RevokeChannel
from hub.domain.channels.entities import (
    ChannelNegotiationIntent,
    ChannelNegotiationOperation,
)
from hub.ports.event_bus import DomainEvent, EventBus
from hub.ports.identity import Clock
from hub.ports.repositories import ChannelLeaseRepository


class HandleChannelSignal:
    def __init__(
        self,
        *,
        channels: ChannelLeaseRepository,
        revoke_channel: RevokeChannel,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._channels = channels
        self._revoke_channel = revoke_channel
        self._events = events
        self._clock = clock

    async def execute(self, signal: ChannelNegotiationIntent) -> None:
        if signal.channel_id is not None:
            lease = await self._channels.get(signal.channel_id)
            if lease is None or lease.device_id != signal.device_id:
                raise PermissionError("channel signal does not belong to device")
            if lease.expires_at <= self._clock.now():
                raise PermissionError("channel lease has expired")
            if signal.operation is ChannelNegotiationOperation.CLOSE:
                await self._revoke_channel.execute(
                    lease.channel_id,
                    reason=signal.reason or "device-closed",
                )
        await self._events.publish(
            DomainEvent(
                event_id=signal.request_id,
                event_type=f"eidolon.{signal.operation.value}.v1",
                source="eidolon-hub/channel-control",
                subject=signal.device_id,
                occurred_at=self._clock.now(),
                data_json=json.dumps(
                    {
                        "channel_id": signal.channel_id,
                        "connection_id": signal.connection_id,
                    },
                    sort_keys=True,
                ),
            )
        )
