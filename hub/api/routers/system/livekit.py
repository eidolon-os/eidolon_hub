"""LiveKit routes - token generation for client connections."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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

    api_key = os.environ.get("LIVEKIT_API_KEY", "devkey")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "devkey_secret")

    if not api_key or not api_secret:
        raise ValueError("LIVEKIT_API_KEY or LIVEKIT_API_SECRET not configured")

    token = (
        api.AccessToken(api_key, api_secret)
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
        .to_jwt()
    )

    return participant_name, token
