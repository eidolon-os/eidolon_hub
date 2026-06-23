"""Eidolon Hub main entry point - ASGI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx
from eidolon_sdk.admin import AdminClient
from eidolon_sdk.adapters.registry_sqlite import DeviceRepository, RegistrySqliteStore
from fastapi import FastAPI

import hub
from hub.api.routers.admin import (
    admin_commands_router,
    admin_devices_router,
    admin_discovery_router,
    admin_events_router,
)
from hub.api.routers.system import config_router
from hub.config import AppConfig, load_config
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.control_bridge import LiveKitControlBridge
from hub.core.device_manager import DeviceManager
from hub.core.discovery import MdnsDiscoveryState, mdns_lifespan
from hub.logging import setup_logging

logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create FastAPI application with Hub lifecycle management."""
    app_config = config or load_config()

    async def lifespan(app: FastAPI) -> None:
        setup_logging(level=app_config.logging.level)
        logger.info("Starting Eidolon Hub v%s", hub.__version__)

        registry_store = RegistrySqliteStore(app_config.storage.registry_db_path)
        device_manager = DeviceManager(DeviceRepository(registry_store))
        await device_manager.load()
        admin_runtime = LiveKitAdminRuntime(app_config)
        control_bridge = LiveKitControlBridge(app_config, admin_runtime)
        admin_runtime.set_control_bridge(control_bridge)
        discovery_state = MdnsDiscoveryState()

        # Phase 32.A: process-wide httpx client + admin REST wrapper
        # used by /api/config (web) to validate user_id + resolve the
        # template before signing the device JWT. trust_env=False so a
        # macOS Clash on :7890 can't intercept the loopback request.
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            trust_env=False,
        )
        admin_client = AdminClient(http_client, app_config.runtime_admin.admin_api_url)

        app.state.device_manager = device_manager
        app.state.registry_store = registry_store
        app.state.admin_runtime = admin_runtime
        app.state.control_bridge = control_bridge
        app.state.discovery_state = discovery_state
        app.state.config = app_config
        app.state.http_client = http_client
        app.state.admin_client = admin_client

        probe_task = None
        await control_bridge.start()
        if app_config.admin.probe_enabled:
            admin_runtime.get_probe_health().running = True

            async def probe_loop() -> None:
                while True:
                    device_ids = [device.device_id for device in device_manager.list_all()]
                    await admin_runtime.run_probe_cycle(device_ids)
                    presence = await admin_runtime.get_presence_snapshot()
                    # I3: only keep the control bridge in rooms of devices that
                    # are still present. Dropping offline devices' rooms makes the
                    # reconciling sync_rooms() disconnect the bridge from them, so
                    # the empty control room is reclaimed (no保活 of dead devices).
                    await control_bridge.sync_rooms(
                        [
                            item.room_name
                            for item in presence
                            if item.room_name and item.status != "offline"
                        ]
                    )
                    await admin_runtime.mark_command_timeout(app_config.admin.command_timeout_seconds)
                    await asyncio.sleep(app_config.admin.probe_interval_seconds)

            probe_task = asyncio.create_task(probe_loop())

        logger.info("Hub started successfully")
        logger.info("  - HTTP API: http://%s:%d", app_config.api.host, app_config.api.port)

        async with mdns_lifespan(
            port=app_config.api.port,
            version=hub.__version__,
            discovery_config=app_config.discovery,
            discovery_state=discovery_state,
        ):
            yield

        logger.info("Stopping Eidolon Hub...")
        if probe_task:
            admin_runtime.get_probe_health().running = False
            probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await probe_task
        await control_bridge.stop()
        await device_manager.save()
        await registry_store.dispose()
        await http_client.aclose()
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

    app.include_router(config_router)
    app.include_router(admin_devices_router)
    app.include_router(admin_discovery_router)
    app.include_router(admin_commands_router)
    app.include_router(admin_events_router)

    return app


def get_app() -> FastAPI:
    """Return ASGI application instance (used by uvicorn)."""
    return create_app()


app = get_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "hub.main:app",
        host=cfg.api.host,
        port=cfg.api.port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
