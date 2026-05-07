from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
