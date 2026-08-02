"""Periodic scheduler for the time-dependent Device Directory projection."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from hub.domain.devices.entities import DeviceDirectoryEntry

logger = logging.getLogger(__name__)


class DirectoryProjector(Protocol):
    async def execute_all(self) -> tuple[DeviceDirectoryEntry, ...]: ...


class DeviceDirectoryProjectionWorker:
    def __init__(
        self,
        projector: DirectoryProjector,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("directory projection interval must be positive")
        self._projector = projector
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="hub-device-directory-projection",
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self._projector.execute_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Device Directory projection cycle failed")
            await asyncio.sleep(self._interval)
