from __future__ import annotations

from fastapi import APIRouter, Request

from hub.api.routers.admin.schemas import DiscoveryStatusResponse
from hub.core.discovery import MdnsDiscoveryState

router = APIRouter(prefix="/api/admin/discovery", tags=["Admin Discovery"])


@router.get("", response_model=DiscoveryStatusResponse)
async def get_discovery_status(request: Request) -> DiscoveryStatusResponse:
    state: MdnsDiscoveryState | None = getattr(
        request.app.state,
        "discovery_state",
        None,
    )
    if state is None:
        state = MdnsDiscoveryState()
    return DiscoveryStatusResponse.model_validate(state.snapshot())
