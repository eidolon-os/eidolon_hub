"""Signed delivery of a companion's generated idle-loop clip to its device.

A device (mobile) plays this looping clip as its resting/idle placeholder while
the agent isn't speaking — the client-side idle path, mirroring the web client
(which fetches the same asset from admin). The clip is generated offline by admin
and stored in Eidolon Data; hub streams it to the bound device over the same
signed-request auth the device already uses, resolving device → companion so the
URL needs no companion id (and a device can only fetch its own companion's clip).
"""

from __future__ import annotations

import hashlib

from eidolon_sdk.biz.admin import (
    AdminResolveNotFound,
    AdminResolvePrecondition,
    AdminResolveUnreachable,
    AdminResolveUpstream,
)
from eidolon_sdk.biz.devices import DeviceAuthHeaders
from fastapi import APIRouter, Header, HTTPException, Request, Response

from hub.api.routers.system.config import _authenticate_signed_device

router = APIRouter(prefix="/api/avatar", tags=["Avatar"])


def _auth_headers(
    *,
    device_id: str | None,
    nonce: str | None,
    timestamp: str | None,
    public_key: str | None,
    signature: str | None,
) -> DeviceAuthHeaders:
    if not device_id:
        raise HTTPException(status_code=422, detail="X-Device-ID header is required")
    missing = [
        name
        for name, value in (
            ("X-Device-Nonce", nonce),
            ("X-Device-Timestamp", timestamp),
            ("X-Device-Signature", signature),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"missing device auth headers: {', '.join(missing)}",
        )
    return DeviceAuthHeaders(
        device_id=device_id,
        nonce=nonce or "",
        timestamp=timestamp or "",
        public_key=public_key,
        signature=signature or "",
    )


@router.get("/idle/video", summary="Signed idle-loop clip for the bound companion")
async def get_companion_idle_video(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    x_device_nonce: str | None = Header(default=None, alias="X-Device-Nonce"),
    x_device_timestamp: str | None = Header(default=None, alias="X-Device-Timestamp"),
    x_device_public_key: str | None = Header(default=None, alias="X-Device-Public-Key"),
    x_device_signature: str | None = Header(default=None, alias="X-Device-Signature"),
) -> Response:
    headers = _auth_headers(
        device_id=x_device_id,
        nonce=x_device_nonce,
        timestamp=x_device_timestamp,
        public_key=x_device_public_key,
        signature=x_device_signature,
    )
    device, _fingerprint, _registration_id = await _authenticate_signed_device(
        request=request,
        device_id=headers.device_id,
        auth_headers=headers,
    )
    if not device.approved:
        raise HTTPException(status_code=412, detail="device is not approved")

    store = getattr(request.app.state, "data_store", None)
    resolve_client = getattr(request.app.state, "admin_resolve_client", None)
    if store is None or resolve_client is None:
        raise HTTPException(status_code=503, detail="hub data/resolve not initialized")

    try:
        resolved = await resolve_client.resolve_device(headers.device_id)
    except AdminResolveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdminResolvePrecondition as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except AdminResolveUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminResolveUpstream as exc:
        raise HTTPException(status_code=502, detail=f"admin upstream: {exc.message}") from exc

    companion_id = getattr(resolved, "companion_id", None)
    if not companion_id:
        raise HTTPException(status_code=404, detail="device is not bound to a companion")

    asset = await store.companion_face_assets.get_active(companion_id)
    if asset is None or asset.idle_status != "ready" or not asset.idle_storage_key:
        raise HTTPException(status_code=404, detail="companion has no idle clip")
    try:
        content = store.object_storage.get(asset.idle_storage_key)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="companion idle clip missing") from exc
    if asset.idle_sha256 and hashlib.sha256(content).hexdigest() != asset.idle_sha256:
        raise HTTPException(status_code=500, detail="idle clip integrity check failed")
    return Response(
        content=content,
        media_type=asset.idle_content_type or "video/mp4",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{asset.idle_sha256}"',
        },
    )
