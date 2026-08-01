"""Periodic runtime for database-backed channel desired-state reconciliation."""

from __future__ import annotations

import asyncio
import logging

from hub.application.use_cases.reconcile_device_channels import ReconcileDeviceChannels

logger = logging.getLogger(__name__)


class DeviceChannelReconcileWorker:
    def __init__(
        self,
        reconciler: ReconcileDeviceChannels,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("channel reconcile interval must be positive")
        self._reconciler = reconciler
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="channel-provider-reconcile")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                result = await self._reconciler.execute()
                if result.failed:
                    logger.warning(
                        "Channel Provider reconciliation had failed devices count=%d",
                        result.failed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Channel Provider reconciliation cycle failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
