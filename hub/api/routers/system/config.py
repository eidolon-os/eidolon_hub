"""Unified LiveKit client configuration: ESP32 and web."""

from __future__ import annotations

import secrets
from enum import Enum

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from hub.api.routers.system.token import AgentMode, TokenResponse, generate_token
from hub.config import load_config

router = APIRouter(prefix="/api", tags=["Config"])


class ClientType(str, Enum):
    ESP32 = "esp32"
    WEB = "web"


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1


class ESP32Config(BaseModel):
    server_url: str
    token: str
    identity: str
    room_name: str
    audio: AudioConfig


class ESP32ConfigResponse(BaseModel):
    success: bool
    config: ESP32Config


def _token_pair(
    room_name: str,
    participant: str,
    agent_mode: AgentMode,
) -> tuple[str, str]:
    try:
        return generate_token(room_name, participant, agent_mode)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _esp32_response(
    *,
    room_name: str | None,
    device_id: str,
    agent_mode: AgentMode,
) -> ESP32ConfigResponse:
    resolved_room = room_name or f"esp32-{secrets.token_hex(4)}"
    _, token = _token_pair(resolved_room, device_id, agent_mode)
    return ESP32ConfigResponse(
        success=True,
        config=ESP32Config(
            server_url=load_config().esp32.server_url,
            token=token,
            identity=device_id,
            room_name=resolved_room,
            audio=AudioConfig(),
        ),
    )


def _web_response(
    *,
    room_name: str,
    participant_name: str,
    agent_mode: AgentMode,
) -> TokenResponse:
    identity, token = _token_pair(room_name, participant_name, agent_mode)
    return TokenResponse(identity=identity, accessToken=token)


@router.get(
    "/config",
    response_model=ESP32ConfigResponse | TokenResponse,
    responses={
        200: {
            "description": "ESP32: wrapped config; Web: token payload",
            "content": {
                "application/json": {
                    "examples": {
                        "esp32": {
                            "summary": "client_type=esp32",
                            "value": {
                                "success": True,
                                "config": {
                                    "server_url": "wss://example",
                                    "token": "jwt",
                                    "identity": "device-id",
                                    "room_name": "room",
                                    "audio": {"sample_rate": 16000, "channels": 1},
                                },
                            },
                        },
                        "web": {
                            "summary": "client_type=web",
                            "value": {"identity": "user-1", "accessToken": "jwt"},
                        },
                    }
                }
            },
        }
    },
)
async def get_config(
    client_type: ClientType = Query(
        default=ClientType.ESP32,
        description="esp32: full device config; web: token only",
    ),
    room_name: str | None = Query(
        default=None,
        description="ESP32: optional room (auto if omitted). Web: required.",
    ),
    participant_name: str | None = Query(
        default=None,
        description="Web participant identity (required when client_type=web)",
    ),
    agent_mode: AgentMode = Query(
        AgentMode.STREAMING,
        description="Agent mode: streaming or ptt",
    ),
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
):
    if client_type == ClientType.ESP32:
        if not x_device_id:
            raise HTTPException(
                status_code=422,
                detail="X-Device-ID header is required when client_type=esp32",
            )
        return _esp32_response(
            room_name=room_name,
            device_id=x_device_id,
            agent_mode=agent_mode,
        )

    if not room_name or not participant_name:
        raise HTTPException(
            status_code=422,
            detail="room_name and participant_name are required when client_type=web",
        )

    return _web_response(
        room_name=room_name,
        participant_name=participant_name,
        agent_mode=agent_mode,
    )
