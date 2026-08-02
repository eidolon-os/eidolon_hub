from __future__ import annotations

import asyncio

from hub.adapters.devices.directory_worker import DeviceDirectoryProjectionWorker


class _Projector:
    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def execute_all(self):
        self.called.set()
        return ()


async def test_projection_worker_runs_immediately_and_stops_cleanly() -> None:
    projector = _Projector()
    worker = DeviceDirectoryProjectionWorker(projector, interval_seconds=60)

    await worker.start()
    await asyncio.wait_for(projector.called.wait(), timeout=0.5)
    await worker.stop()
