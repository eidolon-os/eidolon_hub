from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hub.api.routers.admin import admin_commands_router, admin_devices_router, admin_events_router
from hub.api.routers.system import config_router
from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.device_manager import DeviceManager


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        device_manager = DeviceManager(Path("data/devices.json"))
        await device_manager.load()
        admin_runtime = LiveKitAdminRuntime(config)

        _app.state.device_manager = device_manager
        _app.state.admin_runtime = admin_runtime
        _app.state.config = config

        probe_task = None
        if config.admin.probe_enabled:
            admin_runtime.get_probe_health().running = True

            async def probe_loop() -> None:
                while True:
                    device_ids = [device.device_id for device in device_manager.list_all()]
                    await admin_runtime.run_probe_cycle(device_ids)
                    await admin_runtime.mark_command_timeout(config.admin.command_timeout_seconds)
                    await asyncio.sleep(config.admin.probe_interval_seconds)

            probe_task = asyncio.create_task(probe_loop())

        yield
        if probe_task:
            admin_runtime.get_probe_health().running = False
            probe_task.cancel()
            try:
                await probe_task
            except asyncio.CancelledError:
                pass
        await device_manager.save()

    app = FastAPI(
        lifespan=lifespan,
        title="Eidolon Hub API",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(config_router)
    app.include_router(admin_devices_router)
    app.include_router(admin_commands_router)
    app.include_router(admin_events_router)

    return app
