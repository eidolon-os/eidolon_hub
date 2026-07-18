"""System domain - infrastructure & platform services."""

from hub.api.routers.system.config import router as config_router
from hub.api.routers.system.guard_owner_face import router as guard_owner_face_router
from hub.api.routers.system.sense import router as sense_router

__all__ = ["config_router", "guard_owner_face_router", "sense_router"]
