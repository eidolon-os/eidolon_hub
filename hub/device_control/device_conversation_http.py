"""Scoped Controller access to a directed device conversation."""
from eidolon_sdk.biz.control.device_conversation import DeviceConversationSelection
from fastapi import APIRouter, HTTPException, Request

from hub.admission.domain import AdmissionProblem
from hub.admission.http import problem_response
from hub.channel_reconciliation.domain import ChannelProviderError

from .shared_session_http import CloseSharedSession


def create_device_conversation_router(*, service, actor_provider):
    router = APIRouter(prefix="/api/device-control/v1/device-conversations", tags=["device-control"])

    async def answer(request, call):
        try:
            return await call(await actor_provider(request))
        except AdmissionProblem as exc:
            return problem_response(exc)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ChannelProviderError as exc:
            raise HTTPException(503 if exc.retryable else 409,
                {"code": exc.code, "retryable": exc.retryable}) from exc

    @router.post("/open")
    async def open_session(payload: DeviceConversationSelection, request: Request):
        return await answer(request, lambda context: service().open(payload, context=context))

    @router.post("/status")
    async def status(payload: CloseSharedSession, request: Request):
        return await answer(request, lambda context: service().inspect(payload.session_id, context=context))

    @router.post("/close")
    async def close(payload: CloseSharedSession, request: Request):
        return await answer(request, lambda context: service().inspect(payload.session_id, context=context, close=True))

    return router
