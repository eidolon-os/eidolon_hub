"""Signed, binding-scoped Owner Face Profile delivery endpoints."""

from __future__ import annotations

import hashlib

from eidolon_sdk.biz.devices import DeviceAuthHeaders
from eidolon_sdk.biz.guard import (
    GuardOwnerFaceProfileManifest,
    GuardOwnerFaceReferenceManifest,
)
from fastapi import APIRouter, Header, HTTPException, Request, Response

from hub.api.routers.system.config import _authenticate_signed_device

router = APIRouter(prefix="/api/guard", tags=["Guard"])


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


async def _approved_binding(
    request: Request,
    headers: DeviceAuthHeaders,
):
    device, _fingerprint, _registration_id = await _authenticate_signed_device(
        request=request,
        device_id=headers.device_id,
        auth_headers=headers,
    )
    if not device.approved:
        raise HTTPException(status_code=412, detail="guard device is not approved")
    store = getattr(request.app.state, "data_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="eidolon_data store unavailable")
    binding = await store.guard_bindings.get_active_for_device(headers.device_id)
    if binding is None:
        raise HTTPException(status_code=409, detail="device has no active guard binding")
    return store, binding


@router.get(
    "/owner-face-profile",
    response_model=GuardOwnerFaceProfileManifest,
    summary="Authenticated desired Owner Face Profile manifest",
)
async def get_owner_face_profile(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    x_device_nonce: str | None = Header(default=None, alias="X-Device-Nonce"),
    x_device_timestamp: str | None = Header(default=None, alias="X-Device-Timestamp"),
    x_device_public_key: str | None = Header(default=None, alias="X-Device-Public-Key"),
    x_device_signature: str | None = Header(default=None, alias="X-Device-Signature"),
) -> GuardOwnerFaceProfileManifest:
    headers = _auth_headers(
        device_id=x_device_id,
        nonce=x_device_nonce,
        timestamp=x_device_timestamp,
        public_key=x_device_public_key,
        signature=x_device_signature,
    )
    store, binding = await _approved_binding(request, headers)
    profile = await store.owner_face_profiles.get_desired_for_owner(binding.owner_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="owner face profile is not configured")
    references = []
    for reference in await store.owner_face_profiles.list_references(
        profile.profile_revision_id
    ):
        references.append(
            GuardOwnerFaceReferenceManifest(
                reference_id=reference.reference_id,
                pose=reference.pose,
                sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                content_type=reference.content_type,
            )
        )
    try:
        return GuardOwnerFaceProfileManifest(
            binding_id=binding.binding_id,
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            desired_state=profile.desired_state,
            model_id=profile.model_id,
            preprocessing_version=profile.preprocessing_version,
            references=tuple(references),
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="desired owner face profile is invalid") from exc


@router.get(
    "/owner-face-references/{reference_id}",
    summary="Authenticated normalized Owner Face reference",
)
async def get_owner_face_reference(
    reference_id: str,
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
    store, _binding = await _approved_binding(request, headers)
    resolved = await store.owner_face_profiles.get_reference_for_active_device(
        device_id=headers.device_id,
        reference_id=reference_id,
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="owner face reference not found")
    try:
        content = store.object_storage.get(resolved.storage_key)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="owner face reference is unavailable") from exc
    if (
        len(content) != resolved.size_bytes
        or hashlib.sha256(content).hexdigest() != resolved.sha256
    ):
        raise HTTPException(status_code=500, detail="owner face reference integrity check failed")
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{reference_id}.jpg"',
        },
    )
