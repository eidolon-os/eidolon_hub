"""LiveKit routes - token generation for client connections."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hub.config import load_config

router = APIRouter(prefix="/api/livekit", tags=["LiveKit"])


class TokenResponse(BaseModel):
    identity: str
    accessToken: str


@router.get("/token", response_model=TokenResponse)
async def get_token(
    roomName: str = Query(..., description="LiveKit room name"),
    participantName: str = Query(..., description="Participant identity"),
):
    try:
        identity, token = _generate_token(roomName, participantName)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TokenResponse(identity=identity, accessToken=token)


def _generate_token(room_name: str, participant_name: str) -> tuple[str, str]:
    """Generate a LiveKit access token."""
    from livekit import api

    cfg = load_config().livekit

    if not cfg.api_key or not cfg.api_secret:
        raise ValueError("LIVEKIT_API_KEY or LIVEKIT_API_SECRET not configured")

    token = (
        api.AccessToken(cfg.api_key, cfg.api_secret)
        .with_identity(participant_name)
        .with_name(participant_name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name="eidolon")],
            )
        )
        .to_jwt()
    )

    return participant_name, token
