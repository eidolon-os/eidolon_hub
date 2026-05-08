"""Eidolon Hub main entry point - ASGI application."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI

import hub
from hub.api.routers.admin import admin_commands_router, admin_devices_router, admin_events_router
from hub.api.routers.system import esp32_router, web_router
from hub.config import AppConfig, load_config
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.device_manager import DeviceManager
from hub.core.discovery import mdns_lifespan
from hub.logging import setup_logging

logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create FastAPI application with Hub lifecycle management."""
    app_config = config or load_config()

    async def lifespan(app: FastAPI) -> None:
        setup_logging(level=app_config.logging.level)
        logger.info("Starting Eidolon Hub v%s", hub.__version__)

        device_manager = DeviceManager(Path("data/devices.json"))
        await device_manager.load()
        admin_runtime = LiveKitAdminRuntime(app_config)

        app.state.device_manager = device_manager
        app.state.admin_runtime = admin_runtime
        app.state.config = app_config

        probe_task = None
        if app_config.admin.probe_enabled:
            admin_runtime.get_probe_health().running = True

            async def probe_loop() -> None:
                while True:
                    device_ids = [device.device_id for device in device_manager.list_all()]
                    await admin_runtime.run_probe_cycle(device_ids)
                    await admin_runtime.mark_command_timeout(app_config.admin.command_timeout_seconds)
                    await asyncio.sleep(app_config.admin.probe_interval_seconds)

            probe_task = asyncio.create_task(probe_loop())

        logger.info("Hub started successfully")
        logger.info("  - HTTP API: http://%s:%d", app_config.api.host, app_config.api.port)

        async with mdns_lifespan(
            port=app_config.api.port,
            version=hub.__version__,
            discovery_config=app_config.discovery,
        ):
            yield

        logger.info("Stopping Eidolon Hub...")
        if probe_task:
            admin_runtime.get_probe_health().running = False
            probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await probe_task
        await device_manager.save()
        logger.info("Hub stopped")

    app = FastAPI(
        lifespan=lifespan,
        title="Eidolon Hub API",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        __import__("fastapi.middleware.cors", fromlist=["CORSMiddleware"]).CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(web_router)
    app.include_router(esp32_router)
    app.include_router(admin_devices_router)
    app.include_router(admin_commands_router)
    app.include_router(admin_events_router)

    return app


def get_app() -> FastAPI:
    """Return ASGI application instance (used by uvicorn)."""
    return create_app()


app = get_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "hub.main:app",
        host=os.environ.get("HUB_HOST", "0.0.0.0"),
        port=int(os.environ.get("HUB_PORT", 8000)),
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
