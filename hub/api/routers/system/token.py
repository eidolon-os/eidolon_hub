"""Shared LiveKit token generation and response models."""

from __future__ import annotations

import json
from enum import Enum
from typing import Mapping

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
    participant_metadata: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """Generate a LiveKit access token.

    Args:
        room_name: LiveKit room name.
        participant_name: Participant identity (used as both identity and display name).
        agent_mode: Agent dispatch mode, either "streaming" or "ptt".
        participant_metadata: Optional dict embedded as the participant's
            ``metadata`` field — server-side LiveKit makes it readable to
            other participants in the room (including the channel-worker
            agent). Phase 32.A uses this to ship the device JWT to channel
            without exposing it to the browser JS. ``None`` (default) emits
            no participant metadata so behavior is unchanged for callers
            that don't opt in (esp32 path, tests).

    Returns:
        A tuple of (identity, access_token).
    """
    from livekit import api

    from hub.config import load_config

    cfg = load_config().livekit

    if not cfg.api_key or not cfg.api_secret:
        raise ValueError("LIVEKIT_API_KEY or LIVEKIT_API_SECRET not configured")

    builder = (
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
    )
    if participant_metadata is not None:
        # LiveKit accepts free-form string here; we JSON-encode so channel
        # can parse a structured payload. Empty dict still emits "{}" —
        # that's fine, channel just reads no keys.
        builder = builder.with_metadata(json.dumps(dict(participant_metadata)))

    return participant_name, builder.to_jwt()
