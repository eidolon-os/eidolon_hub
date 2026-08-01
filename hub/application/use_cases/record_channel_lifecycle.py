"""Apply authenticated Channel Provider lifecycle facts to generic leases."""

from __future__ import annotations

from hub.domain.channels.entities import ChannelLifecycle, ChannelState
from hub.ports.repositories import ChannelLeaseRepository


class RecordChannelLifecycle:
    def __init__(self, *, leases: ChannelLeaseRepository) -> None:
        self._leases = leases

    async def execute(self, lifecycle: ChannelLifecycle) -> None:
        lease = await self._leases.get(lifecycle.channel_id)
        if lease is None:
            if lifecycle.state in {ChannelState.CLOSED, ChannelState.FAILED}:
                return
            raise KeyError(lifecycle.channel_id)
        if lease.device_id != lifecycle.device_id:
            raise PermissionError("channel does not belong to lifecycle device")
        updated = lease.transition(lifecycle.state, occurred_at=lifecycle.occurred_at)
        if updated != lease:
            await self._leases.upsert(updated)
