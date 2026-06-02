from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hub.api.clients import AdminClient
from hub.api.routers.admin import admin_commands_router, admin_devices_router, admin_events_router
from hub.api.routers.system import config_router
from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.device_manager import DeviceManager

_log = logging.getLogger(__name__)


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        device_manager = DeviceManager(Path("data/devices.json"))
        await device_manager.load()
        admin_runtime = LiveKitAdminRuntime(config)

        # Phase 32.A: process-wide httpx client for outbound calls
        # (admin lookup at /api/config time). One client → connection
        # pool reuse + clean shutdown semantics. trust_env=False so a
        # macOS Clash on :7890 can't intercept the loopback request
        # (we MUST reach admin directly; HTTP_PROXY would 502).
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            trust_env=False,
        )
        admin_client = AdminClient(http_client, config.runtime_admin.admin_api_url)

        _app.state.device_manager = device_manager
        _app.state.admin_runtime = admin_runtime
        _app.state.config = config
        _app.state.http_client = http_client
        _app.state.admin_client = admin_client

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

        try:
            yield
        finally:
            if probe_task:
                admin_runtime.get_probe_health().running = False
                probe_task.cancel()
                try:
                    await probe_task
                except asyncio.CancelledError:
                    pass
            await device_manager.save()
            await http_client.aclose()

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
