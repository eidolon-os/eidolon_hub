"""Eidolon Hub main entry point - ASGI application."""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from fastapi import FastAPI

import hub
from hub.api.routers.system import esp32_router, web_router
from hub.config import AppConfig, load_config
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
        app.state.device_manager = device_manager
        app.state.config = app_config

        logger.info("Hub started successfully")
        logger.info("  - HTTP API: http://%s:%d", app_config.api.host, app_config.api.port)

        async with mdns_lifespan(
            port=app_config.api.port,
            version=hub.__version__,
        ):
            yield

        logger.info("Stopping Eidolon Hub...")
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
