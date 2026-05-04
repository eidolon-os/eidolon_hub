from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.config import Config
from uvicorn.server import Server

from hub.api.routers.system import esp32_router, livekit_router
from hub.config import AppConfig


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

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

    app.include_router(livekit_router)
    app.include_router(esp32_router)

    return app

class HttpApiRunner:
    def __init__(self, config: AppConfig):
        self._app = create_app(config)
        self._host = config.api.host
        self._port = config.api.port
        self._server = None

    async def start(self) -> None:
        cfg = Config(app=self._app, host=self._host, port=self._port, log_level='info')
        self._server = Server(cfg)
        await self._server.serve()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
