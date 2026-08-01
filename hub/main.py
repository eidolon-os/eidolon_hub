"""Eidolon Hub three-plane ASGI entry point."""

from __future__ import annotations

from fastapi import FastAPI

from hub.composition.app import create_composed_app
from hub.config import load_hub_config


def create_app() -> FastAPI:
    """Create the production device connection and management application."""
    return create_composed_app()


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_hub_config()
    uvicorn.run(
        "hub.main:app",
        host=cfg.api.host,
        port=cfg.api.port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
