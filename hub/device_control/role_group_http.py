"""Team entry point using the same authenticated Controller context as device control."""
from fastapi import APIRouter, Request
from eidolon_sdk.biz.control.coordination import CoordinationSelection, RoleGroupStatus
from .conversation_errors import answer
from .shared_session_http import CloseSharedSession


def create_role_group_router(*, service, actor_provider):
    router = APIRouter(prefix="/api/device-control/v1/role-groups", tags=["device-control"])

    @router.post("/open", response_model=RoleGroupStatus)
    async def start(payload: CoordinationSelection, request: Request):
        return await answer(actor_provider, request, lambda context: service().open(payload, context=context))

    @router.post("/status", response_model=RoleGroupStatus)
    async def status(payload: CloseSharedSession, request: Request):
        return await answer(actor_provider, request, lambda context: service().inspect(payload.session_id, context=context))

    @router.post("/close", response_model=RoleGroupStatus)
    async def close(payload: CloseSharedSession, request: Request):
        return await answer(actor_provider, request, lambda context: service().inspect(payload.session_id, context=context, close=True))

    return router
