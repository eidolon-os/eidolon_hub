"""Host-internal device execution and integration API. No panel or native admission DTOs.

Callers are the Agent (snapshot, execute), the Channel (execute, snapshot,
changes) and Admin on behalf of the phone (providers, accounts, sync). They
share one internal credential; the Owner scope comes from the body, which the
callers have already authenticated.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from eidolon_sdk.biz.smarthome import ExecuteRequest
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .integration_service import AccountService, BindRefused
from .runtime import IdempotencyConflict, SmartHomeRuntime


class HomeScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)


class HomeExecute(HomeScope):
    request: ExecuteRequest


class AccountBind(HomeScope):
    kind: str = Field(min_length=1, max_length=32)
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    fields: dict[str, str] = Field(default_factory=dict)


class ChangesQuery(HomeScope):
    since: int = Field(ge=0, default=0)
    timeout_ms: int = Field(ge=0, le=30_000, default=0)


class ReceiptQuery(HomeScope):
    request_id: str = Field(min_length=1, max_length=128)
    scope: str = ""


def create_smarthome_router(
    service: Callable[[], SmartHomeRuntime | None],
    token: Callable[[], str],
    accounts: Callable[[], AccountService | None] = lambda: None,
):
    router = APIRouter(prefix="/api/smarthome/v1", tags=["smarthome"])

    def authenticate(request: Request) -> None:
        expected = token()
        if not expected or not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + expected
        ):
            raise HTTPException(401, "internal credential was not accepted")

    def runtime(request: Request) -> SmartHomeRuntime:
        authenticate(request)
        value = service()
        if value is None:
            raise HTTPException(503, "smart home is not configured")
        return value

    def account_service(request: Request) -> AccountService:
        authenticate(request)
        value = accounts()
        if value is None:
            raise HTTPException(503, "smart home provider accounts are not configured")
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

    @router.post("/changes")
    async def changes(body: ChangesQuery, request: Request) -> dict[str, Any]:
        rt = runtime(request)
        observed = await rt.observations.changes_since(
            body.owner_id, body.since, body.timeout_ms / 1000
        )
        return {
            "changes": [
                {
                    "device_id": o.target,
                    "reachable": o.reachable,
                    "state": o.state,
                    "observed_at_ms": o.observed_at_ms,
                    "seq": o.seq,
                }
                for o in observed
            ],
            "seq": observed[-1].seq
            if observed
            else await rt.observations.latest_seq(body.owner_id),
        }

    @router.post("/receipts")
    async def receipt(body: ReceiptQuery, request: Request) -> dict[str, Any]:
        found = await runtime(request).ledger.get(body.owner_id, body.scope, body.request_id)
        if found is None:
            raise HTTPException(404, "no receipt for that request")
        return {
            "request_id": found.request_id,
            "scope": found.scope,
            "timestamps": asdict(found.timestamps),
            "result": found.result,
        }

    @router.post("/providers")
    async def providers(body: HomeScope, request: Request) -> dict[str, Any]:
        return account_service(request).providers()

    @router.post("/accounts")
    async def accounts_list(body: HomeScope, request: Request) -> dict[str, Any]:
        return {"accounts": await account_service(request).list(body.owner_id)}

    @router.post("/accounts/bind")
    async def accounts_bind(body: AccountBind, request: Request) -> dict[str, Any]:
        try:
            return await account_service(request).bind(
                body.owner_id, body.kind, body.account_id, body.fields
            )
        except BindRefused as exc:
            raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc

    @router.post("/accounts/{account_id}/unbind")
    async def accounts_unbind(account_id: str, body: HomeScope, request: Request) -> dict[str, Any]:
        removed = await account_service(request).unbind(body.owner_id, account_id)
        if not removed:
            raise HTTPException(404, "no such account for this owner")
        return {"removed": account_id}

    @router.post("/accounts/{account_id}/sync")
    async def accounts_sync(account_id: str, body: HomeScope, request: Request) -> dict[str, Any]:
        try:
            return await account_service(request).sync(body.owner_id, account_id)
        except BindRefused as exc:
            raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
        except KeyError as exc:
            raise HTTPException(404, "no such account for this owner") from exc

    return router
