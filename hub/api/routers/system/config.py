"""Unified LiveKit client configuration: ESP32 and web."""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any, Literal

from eidolon_sdk.biz.admin import (
    AdminClient,
    AdminNotFound,
    AdminResolveClient,
    AdminResolveNotFound,
    AdminResolvePrecondition,
    AdminResolveUnreachable,
    AdminResolveUpstream,
    AdminUnreachable,
    AdminUpstreamError,
)
from eidolon_sdk.biz.body import capability_from_json, capability_to_dict
from eidolon_sdk.biz.contracts import (
    INTERACTION_MODE_FULL_DUPLEX,
    INTERACTION_MODE_HALF_DUPLEX,
    SESSION_INTENT_USER_INITIATED,
    VALID_INTERACTION_MODES,
    VALID_SESSION_INTENTS,
)
from eidolon_sdk.biz.devices import DeviceAuthError, DeviceAuthHeaders, verify_device_signature
from eidolon_sdk.biz.guard import GuardRuntimeConfig
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from hub.api.routers.system.token import AgentMode, TokenResponse, generate_token
from hub.config import AppConfig, load_config, resolve_eidolon_livekit_client_url

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Config"])


class ClientType(str, Enum):
    ESP32 = "esp32"
    WEB = "web"


class ESP32ConfigStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    WAITING_BINDING = "waiting_binding"
    ACTIVE = "active"


PENDING_ROOM_NAME = "eidolon-pending"

# Interaction-mode + session-intent contracts (plan Phase 3/4/5) are sourced
# from the single source ``eidolon_sdk.biz.contracts`` and re-exported above so hub
# and channel can never drift. The device declares both via the
# ``X-Device-Interaction-Mode`` / ``X-Device-Session-Intent`` headers; hub stamps
# the resolved values into the voice token's ``participant_metadata`` and channel
# reads them once per session.


def _normalize_session_intent(
    raw: str | None, *, default: str = SESSION_INTENT_USER_INITIATED
) -> str:
    """Map the (untrusted, unsigned) intent header to a known value.

    Same defense default as interaction_mode: anything missing or unrecognized
    degrades to ``user_initiated`` (a normal session — welcome plays). The header
    is not part of the device signature, so a bad value can only ever produce a
    *less* surprising session, never a spoofed proactive one without a real wake.
    """
    candidate = (raw or "").strip().lower()
    if candidate in VALID_SESSION_INTENTS:
        return candidate
    return default


def _normalize_interaction_mode(raw: str | None, *, default: str) -> str:
    """Map the (untrusted, unsigned) header value to a known mode.

    Defense default (plan §1): anything missing or unrecognized degrades to
    ``default`` — ``half_duplex`` for devices (safe: no accidental barge-in on
    boards with poor AEC), ``full_duplex`` for web. The header is NOT part of
    the device signature (``canonical_request`` excludes it), so it is a hint,
    not a security artifact; the authoritative per-device override is the admin
    path (plan Phase 6).
    """
    candidate = (raw or "").strip().lower()
    if candidate in VALID_INTERACTION_MODES:
        return candidate
    return default


def _admin_interaction_mode_override(resolved: Any) -> str | None:
    """Extract a per-device interaction_mode override from admin's resolve."""
    raw = getattr(resolved, "interaction_mode", None)
    if not raw:
        return None
    candidate = str(raw).strip().lower()
    return candidate if candidate in VALID_INTERACTION_MODES else None


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1


class ESP32ControlConfig(BaseModel):
    server_url: str
    token: str
    identity: str
    room_name: str


class ESP32Config(BaseModel):
    server_url: str
    token: str
    identity: str
    room_name: str
    audio: AudioConfig
    control: ESP32ControlConfig | None = None


class ESP32ConfigResponse(BaseModel):
    success: bool
    status: ESP32ConfigStatus = ESP32ConfigStatus.ACTIVE
    config: ESP32Config
    device: dict[str, Any] | None = None


class GuardRuntimeConfigResponse(BaseModel):
    """Device-local Guard runtime configuration, separate from persona config."""

    success: Literal[True] = True
    schema_v: Literal[1] = 1
    binding_id: str
    guard_companion_id: str
    desired_runtime_state: Literal["running", "stopped"]
    runtime_revision: int = Field(ge=1)
    runtime_config: GuardRuntimeConfig
    control: ESP32ControlConfig


def _token_pair(
    room_name: str,
    participant: str,
    agent_mode: AgentMode,
) -> tuple[str, str]:
    try:
        return generate_token(room_name, participant, agent_mode)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _app_config(request: Request) -> AppConfig:
    cfg = getattr(request.app.state, "config", None)
    return cfg if isinstance(cfg, AppConfig) else load_config()


def _default_active_room_name(device_id: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in device_id.lower())
    safe = "-".join(part for part in safe.split("-") if part)
    return f"device-{safe or 'esp32'}"


def _default_control_room_name(device_id: str) -> str:
    return f"{_default_active_room_name(device_id)}-control"


def _session_voice_room_name(device_id: str) -> str:
    # Per-session voice room: a fresh nonce on every /api/config call so a prior
    # session's late delete-by-name (the old agent deletes device-<id> on
    # device-left / shutdown) can never tear down THIS session's room. The fixed
    # reused name was the root cause of the rapid-rejoin ROOM_DELETED race
    # (JOIN -> X -> quick JOIN -> "Room Deleted", device can't enter). Only the
    # voice room is per-session; the control room stays stable
    # (device-<id>-control), so control-bridge / presence keying is unaffected.
    return f"{_default_active_room_name(device_id)}-{uuid.uuid4().hex[:8]}"


def _server_url(request: Request) -> str:
    cfg = _app_config(request).esp32
    try:
        return resolve_eidolon_livekit_client_url(
            cfg,
            request_host=request.url.hostname or "",
            request_scheme=request.url.scheme or "http",
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _build_device_payload(
    *,
    device_id: str,
    approved: bool,
    bound: bool,
    fingerprint: str = "",
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "approved": approved,
        "bound": bound,
        "fingerprint": fingerprint,
    }


def _pending_esp32_response(
    *,
    request: Request,
    device_id: str,
    status: ESP32ConfigStatus,
    approved: bool,
    fingerprint: str = "",
) -> ESP32ConfigResponse:
    try:
        _identity, token = generate_token(
            room_name=PENDING_ROOM_NAME,
            participant_name=device_id,
            participant_metadata={
                "kind": "device_pending",
                "device_id": device_id,
                "status": status.value,
            },
            dispatch_agent=False,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ESP32ConfigResponse(
        success=True,
        status=status,
        config=ESP32Config(
            server_url=_server_url(request),
            token=token,
            identity=device_id,
            room_name=PENDING_ROOM_NAME,
            audio=AudioConfig(),
        ),
        device=_build_device_payload(
            device_id=device_id,
            approved=approved,
            bound=False,
            fingerprint=fingerprint,
        ),
    )


def _is_registered_guard(device: object) -> bool:
    metadata = getattr(device, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    declaration = metadata.get("guard_manifest")
    return isinstance(declaration, dict) and declaration.get("enabled") is True


def _guard_control_esp32_response(
    *,
    request: Request,
    device_id: str,
    status: ESP32ConfigStatus,
    approved: bool,
    bound: bool,
    fingerprint: str = "",
) -> ESP32ConfigResponse:
    """Return the Guard-only control plane, never a normal voice room.

    A Guard must stay reachable after approval but before a binding exists, so
    it joins its stable control room even while its public status is
    ``waiting_binding``.  The runtime command then causes a signed pull of the
    binding-local configuration.  This avoids treating Guard as a normal slave
    body merely to obtain a LiveKit token.
    """
    room_name = _default_control_room_name(device_id)
    try:
        _identity, token = generate_token(
            room_name=room_name,
            participant_name=device_id,
            participant_metadata={
                "kind": "guard_control",
                "device_id": device_id,
                "status": status.value,
            },
            dispatch_agent=False,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    server_url = _server_url(request)
    control = ESP32ControlConfig(
        server_url=server_url,
        token=token,
        identity=device_id,
        room_name=room_name,
    )
    return ESP32ConfigResponse(
        success=True,
        status=status,
        # Legacy ESP32 response has a mandatory active config.  For Guard it is
        # deliberately the same data-only control room; the Guard firmware
        # suppresses normal voice JOINs at compile time.
        config=ESP32Config(
            server_url=server_url,
            token=token,
            identity=device_id,
            room_name=room_name,
            audio=AudioConfig(),
            control=control,
        ),
        device=_build_device_payload(
            device_id=device_id,
            approved=approved,
            bound=bound,
            fingerprint=fingerprint,
        ),
    )


def _active_esp32_response(
    *,
    request: Request,
    resolved_room: str,
    device_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
    session_intent: str,
    bound: bool,
    fingerprint: str = "",
) -> ESP32ConfigResponse:
    # Tag the ESP32 token with kind=device so channel knows to dispatch
    # /api/resolve/device/{id} (vs /api/resolve/owner for the web flow).
    # Symmetric to _web_response's metadata.
    # Phase 4: carry the device-declared interaction_mode so channel can
    # pick a per-session turn policy (half_duplex -> no barge-in).
    try:
        _identity, token = generate_token(
            room_name=resolved_room,
            participant_name=device_id,
            agent_mode=agent_mode,
            participant_metadata={
                "kind": "device",
                "device_id": device_id,
                "interaction_mode": interaction_mode,
                # Phase 3: why this session exists. proactive_initiated (an
                # orchestrator wake) makes channel suppress the welcome + run the
                # short proactive window; user_initiated is a normal JOIN.
                "session_intent": session_intent,
            },
        )
        _control_identity, control_token = generate_token(
            room_name=_default_control_room_name(device_id),
            participant_name=device_id,
            participant_metadata={
                "kind": "device_control",
                "device_id": device_id,
                "voice_room": resolved_room,
            },
            dispatch_agent=False,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    server_url = _server_url(request)
    return ESP32ConfigResponse(
        success=True,
        status=ESP32ConfigStatus.ACTIVE,
        config=ESP32Config(
            server_url=server_url,
            token=token,
            identity=device_id,
            room_name=resolved_room,
            audio=AudioConfig(),
            control=ESP32ControlConfig(
                server_url=server_url,
                token=control_token,
                identity=device_id,
                room_name=_default_control_room_name(device_id),
            ),
        ),
        device=_build_device_payload(
            device_id=device_id,
            approved=True,
            bound=bound,
            fingerprint=fingerprint,
        ),
    )


_MAX_DECLARED_CAPABILITIES = 64


class DeviceRegisterBody(BaseModel):
    """Body for ``POST /api/device/register`` — the device's self-declared manifest."""

    capabilities: list[dict] = Field(default_factory=list)
    guard: bool = False
    guard_protocol_versions: list[int] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _validate_guard_manifest(self) -> "DeviceRegisterBody":
        if not self.guard and self.guard_protocol_versions:
            raise ValueError("guard_protocol_versions requires guard=true")
        if self.guard and 1 not in self.guard_protocol_versions:
            raise ValueError("guard-capable devices must declare guard protocol version 1")
        return self


def _normalize_capabilities(
    raw: object, *, max_count: int = _MAX_DECLARED_CAPABILITIES
) -> list[dict]:
    """Parse a device capability manifest into the canonical stored shape.

    Drops malformed/nameless entries, dedupes by op name, and caps the count so a
    device can't blow up the tools array. Risky ops stay gated at *actuation* time
    (BodyControlService enforces ``requires_confirmation``), so the hub accepts
    well-formed device-declared ops rather than a fixed allowlist — that is what
    makes the toolset dynamic and open to new device features.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        cap = capability_from_json(item)
        if cap is None or cap.name in seen:
            continue
        seen.add(cap.name)
        out.append(capability_to_dict(cap))
        if len(out) >= max_count:
            break
    return out


def _guard_manifest(body: DeviceRegisterBody) -> dict | None:
    if not body.guard:
        return None
    return {
        "enabled": True,
        "protocol_versions": sorted(set(body.guard_protocol_versions)),
    }


async def _authenticate_signed_device(
    *,
    request: Request,
    device_id: str,
    auth_headers: DeviceAuthHeaders,
    capabilities: list[dict] | None = None,
    guard_manifest: dict | None = None,
    method: str = "GET",
    body: bytes = b"",
):
    """Verify one device request and persist only its Hub registry facts."""
    device_manager = getattr(request.app.state, "device_manager", None)
    if device_manager is None:
        raise HTTPException(
            status_code=503,
            detail="hub device_manager not initialized — restart hub",
        )

    existing = device_manager.get(device_id)
    stored_public_key = None
    if existing is not None:
        stored_public_key = str((existing.metadata or {}).get("public_key") or "") or None
        recent_nonces = (existing.metadata or {}).get("recent_nonces") or []
        if auth_headers.nonce in recent_nonces:
            raise HTTPException(status_code=409, detail="replayed device nonce")

    path_query = request.url.path
    if request.url.query:
        path_query += "?" + request.url.query
    try:
        fingerprint = verify_device_signature(
            headers=auth_headers,
            stored_public_key=stored_public_key,
            path_query=path_query,
            method=method,
            body=body,
        )
    except DeviceAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    public_key = auth_headers.public_key or stored_public_key
    if not public_key:
        raise HTTPException(status_code=401, detail="missing device public key")
    try:
        device = await device_manager.register_signed_seen(
            device_id=device_id,
            public_key=public_key,
            fingerprint=fingerprint,
            nonce=auth_headers.nonce,
            client_ip=request.client.host if request.client else "",
            capabilities=capabilities,
            guard_manifest=guard_manifest,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return device, str((device.metadata or {}).get("fingerprint") or "")


async def _esp32_response(
    *,
    request: Request,
    room_name: str | None,
    device_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
    session_intent: str = SESSION_INTENT_USER_INITIATED,
    auth_headers: DeviceAuthHeaders,
    capabilities: list[dict] | None = None,
    guard_manifest: dict | None = None,
    method: str = "GET",
    body: bytes = b"",
) -> ESP32ConfigResponse:
    device, fingerprint = await _authenticate_signed_device(
        request=request,
        device_id=device_id,
        auth_headers=auth_headers,
        capabilities=capabilities,
        guard_manifest=guard_manifest,
        method=method,
        body=body,
    )

    # A declared Guard has a distinct lifecycle and never resolves through the
    # ordinary persona/voice path.  This branch is intentionally before admin
    # resolve so a missing generic workspace can never prevent its control plane
    # from receiving the binding runtime command.
    if _is_registered_guard(device):
        if not device.approved:
            return _pending_esp32_response(
                request=request,
                device_id=device_id,
                status=ESP32ConfigStatus.PENDING_APPROVAL,
                approved=False,
                fingerprint=fingerprint,
            )
        store = getattr(request.app.state, "data_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="eidolon_data store unavailable")
        binding = await store.guard_bindings.get_active_for_device(device_id)
        return _guard_control_esp32_response(
            request=request,
            device_id=device_id,
            status=(ESP32ConfigStatus.ACTIVE if binding is not None else ESP32ConfigStatus.WAITING_BINDING),
            approved=True,
            bound=binding is not None,
            fingerprint=fingerprint,
        )

    if not device.approved:
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.PENDING_APPROVAL,
            approved=False,
            fingerprint=fingerprint,
        )

    admin_resolve_client: AdminResolveClient | None = getattr(
        request.app.state, "admin_resolve_client", None
    )
    if admin_resolve_client is None:
        raise HTTPException(
            status_code=503,
            detail="hub admin_resolve_client not initialized — restart hub",
        )

    try:
        resolved = await admin_resolve_client.resolve_device(device_id)
    except AdminResolvePrecondition as exc:
        _log.info("device waiting binding device=%s detail=%s", device_id, exc.message)
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.WAITING_BINDING,
            approved=True,
            fingerprint=fingerprint,
        )
    except AdminResolveNotFound as exc:
        _log.info("device waiting binding device=%s detail=%s", device_id, exc)
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.WAITING_BINDING,
            approved=True,
            fingerprint=fingerprint,
        )
    except AdminResolveUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminResolveUpstream as exc:
        raise HTTPException(
            status_code=502, detail=f"admin upstream: {exc.message}"
        ) from exc

    # Phase 6: an admin per-device override (set on the device binding) takes
    # priority over the device's self-declared header. Unset / unknown → keep
    # the (already-defaulted) device-declared value.
    admin_override = _admin_interaction_mode_override(resolved)
    if admin_override is not None:
        interaction_mode = admin_override

    # An explicit ?room_name= override (web / tests) is honored verbatim; the
    # default device path gets a fresh per-session voice room each call.
    resolved_room = room_name or _session_voice_room_name(device_id)
    return _active_esp32_response(
        request=request,
        resolved_room=resolved_room,
        device_id=device_id,
        agent_mode=agent_mode,
        interaction_mode=interaction_mode,
        session_intent=session_intent,
        bound=True,
        fingerprint=fingerprint,
    )


async def _validate_owner_against_admin(
    *,
    admin_client: AdminClient,
    owner_id: str,
) -> str:
    """Confirm ``owner_id`` exists in admin's owner registry.

    Returns the admin-supplied ``display_name`` (used as LK participant
    ``name`` so the room UI shows something friendly). The owner_id
    itself is used as LK ``identity`` — channel reads identity at
    participant-join time and resolves the runtime envelope through
    admin/data before talking to eidolon_agent.

    Raises plain ``AdminClientError`` subclasses; caller wraps to
    HTTPException with appropriate status codes.
    """
    owner_view = await admin_client.get_owner(owner_id)
    if not isinstance(owner_view, dict):
        return owner_id
    return str(owner_view.get("display_name") or owner_id)


async def _web_response(
    *,
    request: Request,
    room_name: str,
    owner_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
) -> TokenResponse:
    """Mint a LiveKit token whose ``identity`` is the admin owner_id.

    Hub does NOT embed any runtime token in participant.metadata. Channel
    reads participant.identity at join time, resolves the runtime identity
    envelope, and signs the short-lived runtime JWT itself.
    """
    admin_client: AdminClient | None = getattr(
        request.app.state, "admin_client", None
    )

    # Admin validation is unconditional. If hub bypasses it, channel will be
    # unable to resolve the participant into a runtime identity envelope.

    if admin_client is None:
        raise HTTPException(
            status_code=503,
            detail="hub admin_client not initialized — restart hub",
        )

    # 1. Fail-fast: refuse to mint LK tokens for owners admin doesn't know.
    try:
        display_name = await _validate_owner_against_admin(
            admin_client=admin_client, owner_id=owner_id
        )
    except AdminNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdminUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminUpstreamError as exc:
        raise HTTPException(
            status_code=502, detail=f"admin upstream: {exc.message}"
        ) from exc

    # 2. LK token: identity is the canonical owner_id; name is the
    #    admin-managed display name (channel doesn't use name, but the
    #    LK web UI / dashboards do).
    try:
        _identity, lk_token = generate_token(
            room_name=room_name,
            participant_name=owner_id,  # -> LK identity
            agent_mode=agent_mode,
            participant_metadata={
                # Identity-derived hints visible inside the room. NOT a
                # security artifact — channel re-fetches authoritatively
                # via admin using participant.identity. ``kind`` lets channel
                # dispatch owner/device resolve paths without try-then-fallback
                # round-trips.
                "kind": "owner",
                "owner_id": owner_id,
                "display_name": display_name,
                "interaction_mode": interaction_mode,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _log.info("issued LK token owner=%s display=%s", owner_id, display_name)
    return TokenResponse(identity=owner_id, accessToken=lk_token)


async def _web_body_response(
    *,
    request: Request,
    room_name: str | None,
    owner_id: str,
    companion_id: str | None,
    device_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
) -> TokenResponse:
    """Mint a LiveKit token for a companion's host-local *web body*.

    Unlike the legacy owner-only web path (identity=owner_id), a web body has a
    real DeviceRow: identity is the ``device_id`` and channel resolves it via
    ``/api/resolve/device/{id}`` — the same path an esp32 body takes, so the
    body runs its companion's persona. There is no browser ECDSA; the binding
    is trusted because admin validated owner+companion+device when the body was
    provisioned. Hub re-checks the binding here so a spoofed ``device_id`` can't
    mint a token for a body it doesn't own.
    """
    admin_resolve_client: AdminResolveClient | None = getattr(
        request.app.state, "admin_resolve_client", None
    )
    if admin_resolve_client is None:
        raise HTTPException(
            status_code=503,
            detail="hub admin_resolve_client not initialized — restart hub",
        )

    try:
        resolved = await admin_resolve_client.resolve_device(device_id)
    except AdminResolveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdminResolvePrecondition as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except AdminResolveUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminResolveUpstream as exc:
        raise HTTPException(
            status_code=502, detail=f"admin upstream: {exc.message}"
        ) from exc

    # Binding checks: the web body must belong to this owner (and to the pinned
    # companion, when the caller supplied one).
    if resolved.owner_id != owner_id:
        raise HTTPException(
            status_code=403, detail="web body is not owned by this owner"
        )
    if companion_id and resolved.companion_id != companion_id:
        raise HTTPException(
            status_code=403, detail="web body is not bound to this companion"
        )

    # A per-device admin override takes priority over the (web-defaulted) header.
    admin_override = _admin_interaction_mode_override(resolved)
    if admin_override is not None:
        interaction_mode = admin_override

    # An explicit ?room_name= override is honored; otherwise a fresh per-session
    # voice room is derived from the device (matches the esp32 body path).
    resolved_room = room_name or _session_voice_room_name(device_id)
    try:
        _identity, lk_token = generate_token(
            room_name=resolved_room,
            participant_name=device_id,  # -> LK identity (kind=device path)
            agent_mode=agent_mode,
            participant_metadata={
                "kind": "device",
                "device_id": device_id,
                "owner_id": owner_id,
                "companion_id": resolved.companion_id,
                "interaction_mode": interaction_mode,
                "session_intent": SESSION_INTENT_USER_INITIATED,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _log.info(
        "issued LK token web-body device=%s owner=%s companion=%s room=%s",
        device_id,
        owner_id,
        resolved.companion_id,
        resolved_room,
    )
    return TokenResponse(identity=device_id, accessToken=lk_token)


@router.post(
    "/device/register",
    response_model=ESP32ConfigResponse,
    summary="Device self-registration: declare capabilities + fetch runtime config",
)
async def register_device(
    request: Request,
    body: DeviceRegisterBody,
    room_name: str | None = Query(default=None),
    agent_mode: AgentMode = Query(AgentMode.STREAMING),
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    x_device_nonce: str | None = Header(default=None, alias="X-Device-Nonce"),
    x_device_timestamp: str | None = Header(default=None, alias="X-Device-Timestamp"),
    x_device_public_key: str | None = Header(default=None, alias="X-Device-Public-Key"),
    x_device_signature: str | None = Header(default=None, alias="X-Device-Signature"),
    x_device_interaction_mode: str | None = Header(
        default=None, alias="X-Device-Interaction-Mode"
    ),
    x_device_session_intent: str | None = Header(
        default=None, alias="X-Device-Session-Intent"
    ),
) -> ESP32ConfigResponse:
    """The mDNS-advertised registration endpoint (``register_url`` TXT).

    A device declares its capability manifest in the signed request body and
    receives its runtime config in the same response — registration and
    activation in one round-trip. Backward compatible: devices that still use
    ``GET /api/config`` keep working unchanged; the declared capabilities are
    persisted to ``devices.capabilities_json`` so the agent exposes each as a tool.
    """
    if not x_device_id:
        raise HTTPException(status_code=422, detail="X-Device-ID header is required")
    missing_auth = [
        name
        for name, value in [
            ("X-Device-Nonce", x_device_nonce),
            ("X-Device-Timestamp", x_device_timestamp),
            ("X-Device-Signature", x_device_signature),
        ]
        if not value
    ]
    if missing_auth:
        raise HTTPException(
            status_code=422,
            detail=f"missing device auth headers: {', '.join(missing_auth)}",
        )
    # The signature covers method + path + the exact request body, so the declared
    # capability manifest is integrity-protected (not a spoofable hint).
    raw_body = await request.body()
    return await _esp32_response(
        request=request,
        room_name=room_name,
        device_id=x_device_id,
        agent_mode=agent_mode,
        interaction_mode=_normalize_interaction_mode(
            x_device_interaction_mode, default=INTERACTION_MODE_HALF_DUPLEX
        ),
        session_intent=_normalize_session_intent(x_device_session_intent),
        auth_headers=DeviceAuthHeaders(
            device_id=x_device_id,
            nonce=x_device_nonce or "",
            timestamp=x_device_timestamp or "",
            public_key=x_device_public_key,
            signature=x_device_signature or "",
        ),
        capabilities=_normalize_capabilities(body.capabilities),
        guard_manifest=_guard_manifest(body),
        method="POST",
        body=raw_body,
    )


@router.get(
    "/guard/runtime-config",
    response_model=GuardRuntimeConfigResponse,
    summary="Authenticated Guard device runtime configuration",
)
async def get_guard_runtime_config(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    x_device_nonce: str | None = Header(default=None, alias="X-Device-Nonce"),
    x_device_timestamp: str | None = Header(default=None, alias="X-Device-Timestamp"),
    x_device_public_key: str | None = Header(default=None, alias="X-Device-Public-Key"),
    x_device_signature: str | None = Header(default=None, alias="X-Device-Signature"),
) -> GuardRuntimeConfigResponse:
    """Return only active binding-local runtime data to its signed device.

    This route intentionally bypasses persona resolution: guard companions do
    not own a genome or memory realm.  P1 Hub policy configuration is never
    included in this response.
    """
    if not x_device_id:
        raise HTTPException(status_code=422, detail="X-Device-ID header is required")
    missing_auth = [
        name
        for name, value in [
            ("X-Device-Nonce", x_device_nonce),
            ("X-Device-Timestamp", x_device_timestamp),
            ("X-Device-Signature", x_device_signature),
        ]
        if not value
    ]
    if missing_auth:
        raise HTTPException(
            status_code=422,
            detail=f"missing device auth headers: {', '.join(missing_auth)}",
        )
    device, _fingerprint = await _authenticate_signed_device(
        request=request,
        device_id=x_device_id,
        auth_headers=DeviceAuthHeaders(
            device_id=x_device_id,
            nonce=x_device_nonce or "",
            timestamp=x_device_timestamp or "",
            public_key=x_device_public_key,
            signature=x_device_signature or "",
        ),
    )
    if not device.approved:
        raise HTTPException(status_code=412, detail="guard device is not approved")
    store = getattr(request.app.state, "data_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="eidolon_data store unavailable")
    binding = await store.guard_bindings.get_active_for_device(x_device_id)
    if binding is None:
        raise HTTPException(status_code=409, detail="device has no active guard binding")
    try:
        runtime_config = GuardRuntimeConfig.model_validate(binding.runtime_config_json or {})
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="active guard runtime config is invalid") from exc
    try:
        _identity, control_token = generate_token(
            room_name=_default_control_room_name(x_device_id),
            participant_name=x_device_id,
            participant_metadata={
                "kind": "guard_control",
                "device_id": x_device_id,
                "guard_companion_id": binding.guard_companion_id,
            },
            dispatch_agent=False,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return GuardRuntimeConfigResponse(
        binding_id=binding.binding_id,
        guard_companion_id=binding.guard_companion_id,
        desired_runtime_state=binding.desired_runtime_state,
        runtime_revision=binding.runtime_revision,
        runtime_config=runtime_config,
        control=ESP32ControlConfig(
            server_url=_server_url(request),
            token=control_token,
            identity=x_device_id,
            room_name=_default_control_room_name(x_device_id),
        ),
    )


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
    request: Request,
    client_type: ClientType = Query(
        default=ClientType.ESP32,
        description="esp32: full device config; web: token only",
    ),
    room_name: str | None = Query(
        default=None,
        description="ESP32: optional room (auto if omitted). Web: required.",
    ),
    owner_id: str | None = Query(
        default=None,
        description=(
            "Web client owner identity. Hub validates owner_id against admin, "
            "uses it as the LiveKit participant identity, and channel resolves "
            "the runtime envelope from this identity."
        ),
    ),
    companion_id: str | None = Query(
        default=None,
        description=(
            "Web body: the companion this web body is bound to. When set with "
            "device_id, hub validates the device is bound to this companion."
        ),
    ),
    device_id: str | None = Query(
        default=None,
        description=(
            "Web body: the DeviceRow id of a companion's host-local web body. "
            "When present with client_type=web, hub mints a device-identity "
            "token (channel resolves via /api/resolve/device/{id}) instead of "
            "the legacy owner-identity token; room_name becomes optional."
        ),
    ),
    agent_mode: AgentMode = Query(
        AgentMode.STREAMING,
        description=(
            "LiveKit agent dispatch mode: streaming or ptt. Duplex/barge-in "
            "capability is declared separately by X-Device-Interaction-Mode."
        ),
    ),
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    x_device_nonce: str | None = Header(default=None, alias="X-Device-Nonce"),
    x_device_timestamp: str | None = Header(default=None, alias="X-Device-Timestamp"),
    x_device_public_key: str | None = Header(default=None, alias="X-Device-Public-Key"),
    x_device_signature: str | None = Header(default=None, alias="X-Device-Signature"),
    x_device_interaction_mode: str | None = Header(
        default=None, alias="X-Device-Interaction-Mode"
    ),
    x_device_session_intent: str | None = Header(
        default=None, alias="X-Device-Session-Intent"
    ),
):
    if client_type == ClientType.ESP32:
        if not x_device_id:
            raise HTTPException(
                status_code=422,
                detail="X-Device-ID header is required when client_type=esp32",
            )
        missing_auth = [
            name
            for name, value in [
                ("X-Device-Nonce", x_device_nonce),
                ("X-Device-Timestamp", x_device_timestamp),
                ("X-Device-Signature", x_device_signature),
            ]
            if not value
        ]
        if missing_auth:
            raise HTTPException(
                status_code=422,
                detail=f"missing device auth headers: {', '.join(missing_auth)}",
            )
        return await _esp32_response(
            request=request,
            room_name=room_name,
            device_id=x_device_id,
            agent_mode=agent_mode,
            interaction_mode=_normalize_interaction_mode(
                x_device_interaction_mode,
                default=INTERACTION_MODE_HALF_DUPLEX,
            ),
            session_intent=_normalize_session_intent(x_device_session_intent),
            auth_headers=DeviceAuthHeaders(
                device_id=x_device_id,
                nonce=x_device_nonce or "",
                timestamp=x_device_timestamp or "",
                public_key=x_device_public_key,
                signature=x_device_signature or "",
            ),
        )

    if not owner_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "owner_id is required when client_type=web; create the owner "
                "in admin UI first if you don't have one."
            ),
        )

    # A managed web *body* (has a DeviceRow): identity is the device, resolved
    # + binding-checked against admin like an esp32 body (no browser ECDSA —
    # admin trust). room_name is optional here; it is derived from the device.
    if device_id:
        return await _web_body_response(
            request=request,
            room_name=room_name,
            owner_id=owner_id,
            companion_id=companion_id,
            device_id=device_id,
            agent_mode=agent_mode,
            interaction_mode=_normalize_interaction_mode(
                x_device_interaction_mode,
                default=INTERACTION_MODE_FULL_DUPLEX,
            ),
        )

    # Legacy owner-only web path (identity = owner_id); room_name required.
    if not room_name:
        raise HTTPException(
            status_code=422,
            detail="room_name is required when client_type=web",
        )

    return await _web_response(
        request=request,
        room_name=room_name,
        owner_id=owner_id,
        agent_mode=agent_mode,
        interaction_mode=_normalize_interaction_mode(
            x_device_interaction_mode,
            default=INTERACTION_MODE_FULL_DUPLEX,
        ),
    )
