"""Shared LiveKit token generation and response models."""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel


class AgentMode(str, Enum):
    STREAMING = "streaming"
    PTT = "ptt"


class TokenResponse(BaseModel):
    identity: str
    accessToken: str


def generate_token(
    room_name: str,
    participant_name: str,
    agent_mode: AgentMode = AgentMode.STREAMING,
) -> tuple[str, str]:
    """Generate a LiveKit access token.

    Args:
        room_name: LiveKit room name.
        participant_name: Participant identity (used as both identity and display name).
        agent_mode: Agent dispatch mode, either "streaming" or "ptt".

    Returns:
        A tuple of (identity, access_token).
    """
    from livekit import api

    from hub.config import load_config

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
                agents=[
                    api.RoomAgentDispatch(
                        agent_name="eidolon",
                        metadata=json.dumps({"agent_mode": agent_mode.value}),
                    )
                ],
            )
        )
        .to_jwt()
    )

    return participant_name, token
