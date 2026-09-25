"""Shared device transport uses the existing scoped Controller authentication."""

from typing import Annotated

from eidolon_sdk.biz.control.shared_session import SharedSessionSelection
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from hub.admission.domain import AdmissionProblem
from hub.admission.http import problem_response
from hub.channel_reconciliation.domain import ChannelProviderError


class CloseSharedSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


def create_shared_session_router(*, service, actor_provider):
    router = APIRouter(prefix="/api/device-control/v1/shared-sessions", tags=["device-control"])

    async def answer(request, call):
        try:
            return await call(await actor_provider(request))
        except AdmissionProblem as exc:
            return problem_response(exc)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ChannelProviderError as exc:
            raise HTTPException(
                503 if exc.retryable else 409, {"code": exc.code, "retryable": exc.retryable}
            ) from exc

    @router.post("/open")
    async def open_session(payload: SharedSessionSelection, request: Request):
        return await answer(request, lambda context: service().open(payload, context=context))

    @router.post("/close")
    async def close_session(payload: CloseSharedSession, request: Request):
        return await answer(
            request, lambda context: service().close(payload.session_id, context=context)
        )

    return router
