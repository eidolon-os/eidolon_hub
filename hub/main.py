"""Eidolon Hub ASGI entry point."""

from __future__ import annotations

from fastapi import FastAPI

from hub.composition.app import create_composed_app


def create_app() -> FastAPI:
    """Create the production device access and management application."""
    return create_composed_app()


app = create_app()
