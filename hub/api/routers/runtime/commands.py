"""Caller-aware device commands used by Eidolon Agent.

The Agent filters tools for UX. This router is the physical authorization
boundary and therefore re-checks every owner, companion, device, policy and
capability fact before dispatching to LiveKit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from eidolon_sdk.biz.body import validate_capability_arguments
from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hub.core.runtime_blackboard import RuntimeCapabilityUnavailable

router = APIRouter(prefix="/api/runtime", tags=["Runtime Commands"])


class RuntimeCommandRequest(BaseModel):
    requester_owner_id: str = Field(min_length=1, max_length=64)
    requester_companion_id: str = Field(min_length=1, max_length=64)
    op: str = Field(min_length=1, max_length=128)
    capability_version: int = Field(ge=1, le=65535)
    source_device_id: str | None = Field(default=None, max_length=128)
    runtime_caller_id: str | None = Field(default=None, max_length=256)
    runtime_session_id: str | None = Field(default=None, max_length=256)
    runtime_trace_id: str | None = Field(default=None, max_length=64)
    runtime_turn_id: str | None = Field(default=None, max_length=64)
    runtime_tool_call_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_ms: int = Field(default=5_000, ge=1_000, le=600_000)
    qos: str = Field(default="result", pattern="^(fire_and_forget|ack|result)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


class RuntimeCommandResponse(BaseModel):
    command_id: str
    device_id: str
    runtime_caller_id: str | None = None
    runtime_session_id: str | None = None
    runtime_trace_id: str | None = None
    runtime_turn_id: str | None = None
    runtime_tool_call_id: str | None = None
    idempotency_key: str | None = None
    source_device_id: str | None = None
    topic: str
    op: str = ""
    capability_version: int | None = None
    status: str
    created_at: str
    payload: dict[str, Any]
    envelope: dict[str, Any] = Field(default_factory=dict)
    ttl_ms: int = 30_000
    qos: str = "ack"
    priority: str = "normal"
    updated_at: str
    error: str = ""
    ack: dict[str, Any] | None = None
    result: Any = None


@router.post("/devices/{device_id}/commands", response_model=RuntimeCommandResponse)
async def send_runtime_device_command(
    device_id: str,
    req: RuntimeCommandRequest,
    request: Request,
    x_eidolon_service_token: str | None = Header(
        default=None,
        alias="X-Eidolon-Service-Token",
    ),
):
    _authorize_service(request, x_eidolon_service_token)
    store = _data_store(request)
    await _authorize_target(
        store,
        requester_owner_id=req.requester_owner_id,
        requester_companion_id=req.requester_companion_id,
        device_id=device_id,
        source_device_id=req.source_device_id,
    )
    runtime = request.app.state.admin_runtime
    command_id = _idempotent_command_id(
        owner_id=req.requester_owner_id,
        idempotency_key=req.idempotency_key,
    )
    existing = await runtime.get_command(command_id)
    if existing is not None:
        _assert_idempotent_request_matches(existing, req=req, device_id=device_id)
        return RuntimeCommandResponse(**existing)

    blackboard = _runtime_blackboard(request)
    try:
        _runtime_device, capability = await blackboard.resolve_current_capability(
            owner_id=req.requester_owner_id,
            requester_companion_id=req.requester_companion_id,
            device_id=device_id,
            capability_name=req.op,
            capability_version=req.capability_version,
        )
    except RuntimeCapabilityUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="runtime device blackboard unavailable",
        ) from exc
    error = validate_capability_arguments(capability.input_schema, req.payload)
    if error:
        raise HTTPException(status_code=422, detail=error)

    try:
        command = await runtime.send_command(
            device_id=device_id,
            payload=req.payload,
            topic=CONTROL_TOPIC,
            command_id=command_id,
            op=req.op,
            capability_version=req.capability_version,
            capability_contract={
                "name": capability.name,
                "version": capability.version,
                "manifest_revision": _runtime_device.manifest_revision,
                "result_schema": capability.result_schema,
            },
            requester_owner_id=req.requester_owner_id,
            requester_companion_id=req.requester_companion_id,
            source_device_id=req.source_device_id,
            runtime_caller_id=req.runtime_caller_id,
            runtime_session_id=req.runtime_session_id,
            runtime_trace_id=req.runtime_trace_id,
            runtime_turn_id=req.runtime_turn_id,
            runtime_tool_call_id=req.runtime_tool_call_id,
            idempotency_key=req.idempotency_key,
            ttl_ms=req.ttl_ms,
            qos=req.qos,
            priority=req.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _assert_idempotent_request_matches(command, req=req, device_id=device_id)
    return RuntimeCommandResponse(**command)


@router.get("/commands/{command_id}", response_model=RuntimeCommandResponse)
async def get_runtime_command(
    command_id: str,
    request: Request,
    requester_owner_id: str = Query(min_length=1, max_length=64),
    requester_companion_id: str = Query(min_length=1, max_length=64),
    x_eidolon_service_token: str | None = Header(
        default=None,
        alias="X-Eidolon-Service-Token",
    ),
):
    _authorize_service(request, x_eidolon_service_token)
    store = _data_store(request)
    await _authorize_requester(store, requester_owner_id, requester_companion_id)
    command = await request.app.state.admin_runtime.get_command(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail=f"Command not found: {command_id}")
    if command.get("owner_id") != requester_owner_id:
        raise HTTPException(status_code=403, detail="command belongs to another owner")
    return RuntimeCommandResponse(**command)


def _authorize_service(request: Request, provided: str | None) -> None:
    expected = os.environ.get("EIDOLON_RUNTIME_SERVICE_TOKEN", "").strip()
    if expected:
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid runtime service token")
        return
    host = str(request.client.host if request.client else "")
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status_code=403,
            detail="runtime API is loopback-only when no service token is configured",
        )


def _idempotent_command_id(*, owner_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{owner_id}\0{idempotency_key}".encode("utf-8")).hexdigest()[:48]
    return f"rcmd_{digest}"


def _assert_idempotent_request_matches(
    command: dict[str, Any],
    *,
    req: RuntimeCommandRequest,
    device_id: str,
) -> None:
    expected = {
        "owner_id": req.requester_owner_id,
        "requester_companion_id": req.requester_companion_id,
        "device_id": device_id,
        "op": req.op,
        "capability_version": req.capability_version,
        "payload": req.payload,
        "idempotency_key": req.idempotency_key,
    }
    actual = {
        "owner_id": command.get("owner_id") or command.get("requester_owner_id"),
        "requester_companion_id": command.get("requester_companion_id"),
        "device_id": command.get("device_id"),
        "op": command.get("op"),
        "capability_version": command.get("capability_version"),
        "payload": command.get("payload") or {},
        "idempotency_key": command.get("idempotency_key"),
    }
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                "idempotency key was already used for a different runtime command: "
                + json.dumps(actual, ensure_ascii=False, sort_keys=True)
            ),
        )


def _data_store(request: Request):
    store = getattr(request.app.state, "data_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="eidolon_data store unavailable")
    return store


def _runtime_blackboard(request: Request):
    blackboard = getattr(request.app.state, "runtime_blackboard", None)
    if blackboard is None:
        raise HTTPException(status_code=503, detail="runtime blackboard unavailable")
    return blackboard


async def _authorize_requester(store, owner_id: str, companion_id: str):
    companion = await store.companions.get(companion_id)
    if companion is None or companion.owner_id != owner_id or companion.status != "active":
        raise HTTPException(status_code=403, detail="requester companion is not active for owner")
    return companion


async def _authorize_target(
    store,
    *,
    requester_owner_id: str,
    requester_companion_id: str,
    device_id: str,
    source_device_id: str | None,
):
    await _authorize_requester(store, requester_owner_id, requester_companion_id)
    target = await store.devices.get_device(device_id)
    if target is None or target.owner_id != requester_owner_id:
        raise HTTPException(status_code=403, detail="target device belongs to another owner")
    if target.revoked_at is not None or target.status in {"disabled", "revoked"}:
        raise HTTPException(status_code=403, detail="target device is disabled or revoked")
    if not target.bound_companion_id:
        raise HTTPException(status_code=409, detail="target device is not bound to a companion")
    provider = await store.companions.get(target.bound_companion_id)
    if provider is None or provider.owner_id != requester_owner_id or provider.status != "active":
        raise HTTPException(status_code=409, detail="provider companion is not active")

    policy = target.access_policy_json or {}
    visibility = str(policy.get("capability_visibility") or "owner")
    if visibility == "bound_companion":
        if target.bound_companion_id != requester_companion_id:
            raise HTTPException(status_code=403, detail="device capabilities are private")
    elif visibility != "owner":
        raise HTTPException(status_code=403, detail="device capability visibility is invalid")

    if source_device_id:
        source = await store.devices.get_device(source_device_id)
        if (
            source is None
            or source.owner_id != requester_owner_id
            or source.bound_companion_id != requester_companion_id
            or source.revoked_at is not None
            or source.status in {"disabled", "revoked"}
        ):
            raise HTTPException(status_code=403, detail="source device is not valid for requester")
    return target
