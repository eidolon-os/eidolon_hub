"""Web routes - token generation for client connections."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hub.api.routers.system.token import AgentMode, TokenResponse, generate_token

router = APIRouter(prefix="/api/web", tags=["Web"])


@router.get("/config", response_model=TokenResponse)
async def get_token(
    room_name: str = Query(..., description="LiveKit room name"),
    participant_name: str = Query(..., description="Participant identity"),
    agent_mode: AgentMode = Query(AgentMode.STREAMING, description="Agent mode: 'streaming' or 'ptt'"),
):
    try:
        identity, token = generate_token(room_name, participant_name, agent_mode)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TokenResponse(identity=identity, accessToken=token)
