"""System domain - infrastructure & platform services."""

from hub.api.routers.system.esp32 import router as esp32_router
from hub.api.routers.system.web import router as web_router

__all__ = ["web_router", "esp32_router"]
