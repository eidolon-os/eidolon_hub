"""Shared LiveKit token generation and response models."""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from eidolon_sdk.livekit import build_livekit_token
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
    *,
    dispatch_agent: bool = True,
    can_publish: bool = True,
    can_subscribe: bool = True,
    can_publish_data: bool = True,
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
        dispatch_agent: Whether LiveKit should dispatch the Eidolon channel
            worker for this room. Pending-device rooms set this to false.
        can_publish: Media publish grant. Pending-device rooms keep this false.
        can_subscribe: Media/data subscribe grant.
        can_publish_data: Data-channel publish grant, useful for command ack.

    Returns:
        A tuple of (identity, access_token).
    """
    from hub.config import load_config

    cfg = load_config().livekit
    token = build_livekit_token(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        room_name=room_name,
        identity=participant_name,
        name=participant_name,
        participant_metadata=participant_metadata,
        dispatch_agent=dispatch_agent,
        agent_name="eidolon",
        agent_metadata={"agent_mode": agent_mode.value} if dispatch_agent else None,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=can_publish_data,
    )

    return participant_name, token
