"""Recoverable delivery of Claim effects to the Channel bounded context."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from hub.domain.channels.entities import ProviderChannelRevocation
from hub.ports.channels import ChannelProviderControl
from hub.ports.device_control import DeviceControlStore
from hub.ports.identity import Clock

_LOG = logging.getLogger(__name__)


class DeliverDeviceControlOperations:
    """Project Claim events, deliver due operations and persist every outcome."""

    def __init__(
        self,
        *,
        store: DeviceControlStore,
        provider: ChannelProviderControl,
        clock: Clock,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds

    async def execute(self, *, limit: int = 50) -> int:
        now = self._clock.now()
        await self._store.materialize_claim_events(now=now)
        delivered = 0
        for operation in await self._store.list_due(now=now, limit=limit):
            try:
                await self._provider.revoke_channels(
                    ProviderChannelRevocation(
                        operation_id=operation.operation_id,
                        owner_domain_id=str(operation.device_ref.owner_domain_id),
                        device_id=operation.device_ref.device_instance_id,
                        reason=operation.reason,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - infrastructure retry boundary
                attempt = operation.attempt_count + 1
                delay = min(self._retry_max, self._retry_base * (2 ** min(attempt - 1, 10)))
                await self._store.mark_retry(
                    event_id=operation.event_id,
                    attempt_count=attempt,
                    next_attempt_at=now + timedelta(seconds=delay),
                    error=type(exc).__name__,
                )
                _LOG.warning(
                    "device control delivery deferred event_id=%s attempt=%d error=%s",
                    operation.event_id,
                    attempt,
                    type(exc).__name__,
                )
                continue
            await self._store.mark_delivered(
                event_id=operation.event_id,
                delivered_at=self._clock.now(),
            )
            delivered += 1
        return delivered


class PeriodicDeviceControlDelivery:
    def __init__(self, delivery: DeliverDeviceControlOperations, *, interval_seconds: float):
        self._delivery = delivery
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name="eidolon-hub-device-control-delivery"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._delivery.execute()
            except Exception:  # noqa: BLE001 - keep the recoverable worker alive
                _LOG.exception("device control delivery pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
