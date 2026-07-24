"""System domain - infrastructure & platform services."""

from hub.api.routers.system.avatar_idle import router as avatar_idle_router
from hub.api.routers.system.config import router as config_router
from hub.api.routers.system.guard_owner_face import router as guard_owner_face_router
from hub.api.routers.system.sense import router as sense_router

__all__ = [
    "avatar_idle_router",
    "config_router",
    "guard_owner_face_router",
    "sense_router",
]
