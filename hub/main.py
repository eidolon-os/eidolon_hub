"""Eidolon Hub 主入口."""

import asyncio
import logging
import signals
from pathlib import Path

from hub.config import AppConfig, load_config
from hub.core.device_manager import DeviceManager
from hub.logging import setup_logging

logger = logging.getLogger(__name__)


class HubApplication:
    """Hub 主应用."""

    def __init__(self, config: AppConfig | None = None):
        self._config = config or load_config()
        self._running = False
        self._device_manager = DeviceManager(Path("data/devices.json"))
        self._http_runner = None

    async def start(self) -> None:
        setup_logging(
            level=self._config.logging.level,
            format_str=self._config.logging.format,
        )
        logger.info("Starting Eidolon Hub v%s", __import__("hub").__version__)

        await self._device_manager.load()

        if self._config.api.enabled:
            from hub.api.app import HttpApiRunner
            self._http_runner = HttpApiRunner(self._config, self._device_manager)
            asyncio.create_task(self._http_runner.start())

        self._running = True
        logger.info("Hub started successfully")
        logger.info("  - HTTP API: http://%s:%d", self._config.api.host, self._config.api.port)

    async def stop(self) -> None:
        logger.info("Stopping Eidolon Hub...")
        self._running = False
        if self._http_runner is not None:
            await self._http_runner.stop()
        await self._device_manager.save()
        logger.info("Hub stopped")

    async def run(self) -> None:
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()


async def main() -> None:
    app = HubApplication()
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    asyncio.create_task(app.run())
    await stop_event.wait()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())