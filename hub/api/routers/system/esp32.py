"""ESP32 device configuration endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from hub.api.routers.system.livekit import _generate_token
from hub.config import load_config

router = APIRouter(prefix="/esp32/livekit", tags=["ESP32"])


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


@router.get("/config", response_model=ESP32ConfigResponse)
async def get_esp32_config(
    x_device_id: str = Header(..., alias="X-Device-ID"),
    room_name: str = Query(..., description="LiveKit room name"),
):
    """Return LiveKit configuration for an ESP32 device."""
    server_url = load_config().esp32.server_url

    try:
        _, token = _generate_token(room_name, x_device_id)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ESP32ConfigResponse(
        success=True,
        config=ESP32Config(
            server_url=server_url,
            token=token,
            identity=x_device_id,
            room_name=room_name,
            audio=AudioConfig(),
        ),
    )
