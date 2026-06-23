"""Admin domain routers."""

from hub.api.routers.admin.commands import router as admin_commands_router
from hub.api.routers.admin.devices import router as admin_devices_router
from hub.api.routers.admin.discovery import router as admin_discovery_router
from hub.api.routers.admin.events import router as admin_events_router

__all__ = [
    "admin_commands_router",
    "admin_devices_router",
    "admin_discovery_router",
    "admin_events_router",
]
