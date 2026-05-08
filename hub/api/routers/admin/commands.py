from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from hub.api.routers.admin.schemas import CommandListResponse, CommandRequest, CommandResponse

router = APIRouter(prefix="/api/admin", tags=["Admin Commands"])


@router.post("/devices/{device_id}/commands", response_model=CommandResponse)
async def send_device_command(
    device_id: str,
    req: CommandRequest,
    request: Request,
):
    runtime = request.app.state.admin_runtime
    try:
        command = await runtime.send_command(device_id=device_id, payload=req.payload, topic=req.topic)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return CommandResponse(**command)


@router.get("/commands/{command_id}", response_model=CommandResponse)
async def get_command(command_id: str, request: Request):
    runtime = request.app.state.admin_runtime
    command = runtime.get_command(command_id)
    if not command:
        raise HTTPException(status_code=404, detail=f"Command not found: {command_id}")
    return CommandResponse(**command)


@router.get("/commands", response_model=CommandListResponse)
async def list_commands(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
):
    runtime = request.app.state.admin_runtime
    commands = runtime.list_commands(limit=limit)
    return CommandListResponse(commands=[CommandResponse(**item) for item in commands])
