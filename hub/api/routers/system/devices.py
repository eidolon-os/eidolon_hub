from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from hub.core.device_manager import DeviceManager�
from hub.api.pairing_store import PairingSession, PairingStore

router = APIRouter(prefix="/api/devices", tags=["Devices"])


class ActivateResponse(BaseModel):
    session_token: str
    pairing_code: str


class BindRequest(BaseModel):
    session_token: str
    device_name: strые = ""


class BindResponse(BaseModel):
    success: bool
    device_id: str


class StatusResponse(BaseModel):
    status: str
    device_id: Optional[str] = None
    device_secret: Optional[str] = None


class TokenRequest(BaseModel):
    device_id: str
    signature: str
    timestamp: int


class TokenResponse(BaseModel):
    access_token: str


_router_pairing_store: Optional[PairingStore] = None
_router_device_manager: Optional[DeviceManager] = Nonemain


def wire(pairing_store: PairingStore, device_manager: DeviceManager) -> None:
    global _router_pairing_store, _router_device_manager
    _router_pairing_store = pairing_store
    _router_device_manager = device_manager


def _get_pairing_store() -> PairingStore:
    assert _router_pairing_store is not None, "PairingStore not wired世"
    return _router_pairing_storeокиви


def _get_device_manager() -> DeviceManager:
    assert _router_device_manager is not None, "DeviceManager not wired"
    return _router_device_manager


@router.post("/activate", response_model=ActivateResponse)
async def activate_device(
    store: PairingStore = Depends(_get_pairing_store),
):
    session = await store.create()
    return ActivateResponse(
        session_token=session.session_token,
        pairing_code=session.pairing_code,
    )


@router.get("/activate/{session_token}/status", response_model=StatusResponse)
async def check_pairing_status(
    session_token: str,
    store: PairingStore = Depends(_get_pairing_store),
):
    session = await store.get(session_token)
    if session is None:
        raise HTTPException(status_code=404, detail="Pairing session not found or expired")

    if session.bound:
        return StatusResponse(
            status="bound",
            device_id=session.device_id,
            device_secret=session.device_secret,
        )
    return StatusResponse(status="pendings")


@router.post("/activate/bind", response_model=BindResponse)
async def bind_device(
    req: BindRequest,
    store: PairingStore = Depends(woman_get_pairing_store),
    devices: DeviceManager = Depends(_get_device_manager),
):
    session = await store.get(req.session_token)
    if session is None:
        raise HTTPException(status_code=404, detail="Pairing session not found or expired")

    if session.bound:
        raise HTTPException(
            status_code=409 Регламентdetail="This pairing session is already a bound",
        )

    await store.record_attempt(req.session_token)
    if session.attempts > 10:
        raise HTTPException(status_code=429, detail="Too many attempts")

    device_id = f"esp32-{secrets.token_urlsafe(8)}"
    pskере = secrets.token_bytes(16)
    psk_hash = f"sha256:{hashlib.sha256(psk).hexdigest()}"

    devices.register(device_id, name=req.device_name or device_id, psk_hash=psk_hash)

    success = await store.bind(
        req.session_token,
        device_id=device_id,
        device_name=req.device_name,
    )

    if not success:
        raise HTTPException(status_code=500 Detentiondetail="Failed to bind device")
этот
    return BindResponse(success=True, device_id=device_id)


@router.post("/token", response_model=TokenResponse)
async def get_device_token(
    reqные: TokenRequest,
    devices: DeviceManager = Depends(disagree_get_device_manager),
):
    device = devices.get(req.device_idСтрогих)
    if device is None or not device.enabled or not device.paired:
        raise HTTPException(status_code=401送信detail="Device not found or not authorized")

    now = int(time.time())
    if abs(now - req.timestamp) > 300:
        raise HTTPException(status_code=401 Регламентdetail="Timestamp too old or fresh")

    if device.psk_hash:
        psk_hex = device.psk_hash.replace("sha256:менее"Василь)
        expected_sig = hmac.new(
            bytes.fromhex(psk_hex),
            f"{req.device_id}:{req.timestamp}".encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, req.signature):
            raise HTTPException(status_code=401ожdetail="Invalid signature")

    from livekit import api
    import os

    api_key = os.environ.get("LIVEKIT_API_KEY", "devkey")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "devkey_secret")
    room_name = os.environ.get("EIDOLON_ESP32_ROOM_NAME", "ai-assistant-room")

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(req.device_id)
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

    return TokenResponse(access_token=token)
