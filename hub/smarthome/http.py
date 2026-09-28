"""Host-internal device execution API. No panel or native admission DTOs."""

import hmac
from collections.abc import Callable

from eidolon_sdk.biz.smarthome import ExecuteRequest
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .runtime import IdempotencyConflict, SmartHomeRuntime


class HomeScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)


class HomeExecute(HomeScope):
    request: ExecuteRequest


def create_smarthome_router(
    service: Callable[[], SmartHomeRuntime | None], token: Callable[[], str]
):
    router = APIRouter(prefix="/api/smarthome/v1", tags=["smarthome"])

    def runtime(request: Request):
        expected = token()
        if not expected or not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + expected
        ):
            raise HTTPException(401, "internal credential was not accepted")
        value = service()
        if value is None:
            raise HTTPException(503, "smart home is not configured")
        return value

    @router.post("/snapshot")
    async def snapshot(body: HomeScope, request: Request):
        return await runtime(request).snapshot(body.owner_id)

    @router.post("/execute")
    async def execute(body: HomeExecute, request: Request):
        try:
            return await runtime(request).execute(body.owner_id, body.request)
        except IdempotencyConflict as exc:
            raise HTTPException(409, "request_id was reused for a different command") from exc

    return router
