"""Unified LiveKit client configuration: ESP32 and web."""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any

from eidolon_sdk.admin import (
    AdminClient,
    AdminNotFound,
    AdminPrecondition,
    AdminUnreachable,
    AdminUpstreamError,
)
from eidolon_sdk.devices import DeviceAuthError, DeviceAuthHeaders, verify_device_signature
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

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

# Interaction-mode contract (see plan Phase 4/5). The device declares its
# capability via the ``X-Device-Interaction-Mode`` header; hub stamps the
# resolved mode into the LiveKit token's ``participant_metadata`` so channel
# can pick a per-session turn policy. ``half_duplex`` = push-to-talk (mic
# closed during playback, explicit turn boundary); ``full_duplex`` = open mic
# with hardware AEC (server-judged barge-in).
INTERACTION_MODE_HALF_DUPLEX = "half_duplex"
INTERACTION_MODE_FULL_DUPLEX = "full_duplex"
_VALID_INTERACTION_MODES = frozenset(
    {INTERACTION_MODE_HALF_DUPLEX, INTERACTION_MODE_FULL_DUPLEX}
)


# Session-intent contract (plan §3.2 / Phase 3). The device declares why this
# voice session exists via the ``X-Device-Session-Intent`` header; hub stamps the
# resolved value into the voice token's ``participant_metadata`` so channel can
# suppress the welcome + run the proactive short-window. ``proactive_initiated``
# = an orchestrator-driven wake (a long task finished); ``user_initiated`` = a
# normal user JOIN. Values MUST match channel's ``_VALID_INTENTS``.
SESSION_INTENT_USER_INITIATED = "user_initiated"
SESSION_INTENT_PROACTIVE = "proactive_initiated"
_VALID_SESSION_INTENTS = frozenset(
    {SESSION_INTENT_USER_INITIATED, SESSION_INTENT_PROACTIVE}
)


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
    if candidate in _VALID_SESSION_INTENTS:
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
    if candidate in _VALID_INTERACTION_MODES:
        return candidate
    return default


def _admin_interaction_mode_override(resolved: Any) -> str | None:
    """Extract a per-device interaction_mode override from admin's resolve
    response (Phase 6). Returns a valid mode, or ``None`` when admin set no
    override (or returned an unrecognized value — we don't let a bad admin
    value override the device's own declaration)."""
    if not isinstance(resolved, dict):
        return None
    context = resolved.get("context")
    context = context if isinstance(context, dict) else resolved
    raw = context.get("interaction_mode")
    if not raw:
        return None
    candidate = str(raw).strip().lower()
    return candidate if candidate in _VALID_INTERACTION_MODES else None


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


async def _esp32_response(
    *,
    request: Request,
    room_name: str | None,
    device_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
    session_intent: str = SESSION_INTENT_USER_INITIATED,
    auth_headers: DeviceAuthHeaders,
) -> ESP32ConfigResponse:
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    fingerprint = str((device.metadata or {}).get("fingerprint") or "")

    if not device.approved:
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.PENDING_APPROVAL,
            approved=False,
            fingerprint=fingerprint,
        )

    admin_client: AdminClient | None = getattr(request.app.state, "admin_client", None)
    if admin_client is None:
        raise HTTPException(
            status_code=503,
            detail="hub admin_client not initialized — restart hub",
        )

    try:
        resolved = await admin_client.resolve_device(device_id)
    except AdminPrecondition as exc:
        _log.info("device waiting binding device=%s detail=%s", device_id, exc.message)
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.WAITING_BINDING,
            approved=True,
            fingerprint=fingerprint,
        )
    except AdminNotFound as exc:
        _log.warning("device resolve not found device=%s detail=%s", device_id, exc)
        return _pending_esp32_response(
            request=request,
            device_id=device_id,
            status=ESP32ConfigStatus.WAITING_BINDING,
            approved=True,
            fingerprint=fingerprint,
        )
    except AdminUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminUpstreamError as exc:
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
    # Phase 32.B: tag the ESP32 token with kind=device so channel knows
    # to dispatch /api/resolve/device/{id} (vs /api/resolve/user for
    # the web flow). Symmetric to _web_response's metadata.
    # Phase 4: carry the device-declared interaction_mode so channel can
    # pick a per-session turn policy (half_duplex → no barge-in).
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
            bound=True,
            fingerprint=fingerprint,
        ),
    )


async def _validate_user_against_admin(
    *,
    admin_client: AdminClient,
    user_id: str,
) -> str:
    """Phase 32.A: confirm ``user_id`` exists in admin's registry.

    Returns the admin-supplied ``display_name`` (used as LK participant
    ``name`` so the room UI shows something friendly). The user_id
    itself is used as LK ``identity`` — channel reads identity at
    participant-join time and runs its own admin lookup to find
    tenant/active_agent (plan D).

    Raises plain ``AdminClientError`` subclasses; caller wraps to
    HTTPException with appropriate status codes.
    """
    user_view = await admin_client.get_user(user_id)
    spec = user_view.get("spec", {}) if isinstance(user_view, dict) else {}
    return str(spec.get("display_name") or user_id)


async def _web_response(
    *,
    request: Request,
    room_name: str,
    user_id: str,
    agent_mode: AgentMode,
    interaction_mode: str,
) -> TokenResponse:
    """Mint a LiveKit token whose ``identity`` is the admin user_id.

    Plan D: hub does NOT embed any runtime token in participant.metadata.
    channel reads participant.identity at join time and signs the
    device JWT itself (it shares ``PAIRING_JWT_SECRET`` with agent).
    """
    admin_client: AdminClient | None = getattr(
        request.app.state, "admin_client", None
    )

    # Phase 33.A6: enabled rollback removed — admin validation is now
    # unconditional. Channel 32.D already removed its symmetric
    # static-token fallback, so any hub bypass would only mint a
    # doomed LK token (channel /api/resolve would 404 the next step).

    if admin_client is None:
        raise HTTPException(
            status_code=503,
            detail="hub admin_client not initialized — restart hub",
        )

    # 1. Fail-fast: refuse to mint LK tokens for users admin doesn't know.
    try:
        display_name = await _validate_user_against_admin(
            admin_client=admin_client, user_id=user_id
        )
    except AdminNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdminUnreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AdminUpstreamError as exc:
        raise HTTPException(
            status_code=502, detail=f"admin upstream: {exc.message}"
        ) from exc

    # 2. LK token: identity is the canonical user_id; name is the
    #    admin-managed display name (channel doesn't use name, but the
    #    LK web UI / dashboards do).
    try:
        _identity, lk_token = generate_token(
            room_name=room_name,
            participant_name=user_id,  # → LK identity
            agent_mode=agent_mode,
            participant_metadata={
                # Identity-derived hints visible inside the room. NOT a
                # security artifact — channel re-fetches authoritatively
                # via admin using participant.identity. ``kind`` lets
                # channel dispatch /api/resolve/user vs /api/resolve/device
                # without try-then-fallback round-trips.
                "kind": "user",
                "user_id": user_id,
                "display_name": display_name,
                "interaction_mode": interaction_mode,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _log.info("issued LK token user=%s display=%s", user_id, display_name)
    return TokenResponse(identity=user_id, accessToken=lk_token)


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
    user_id: str | None = Query(
        default=None,
        description=(
            "Phase 32.A: web client must pass the admin-managed user_id "
            "the conversation should be attributed to. Becomes LiveKit "
            "participant identity; channel resolves tenant/agent from "
            "this identity by querying admin (plan D)."
        ),
    ),
    agent_mode: AgentMode = Query(
        AgentMode.STREAMING,
        description="Agent mode: streaming or ptt",
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

    if not room_name:
        raise HTTPException(
            status_code=422,
            detail="room_name is required when client_type=web",
        )

    # Phase 33.A6: user_id is unconditionally required for web — the
    # rollback path that allowed bypass was removed because channel
    # 32.D no longer has a matching static-token fallback anyway.
    if not user_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "user_id is required when client_type=web (Phase 32.A); "
                "create the user in admin UI first if you don't have one."
            ),
        )

    return await _web_response(
        request=request,
        room_name=room_name,
        user_id=user_id,
        agent_mode=agent_mode,
        interaction_mode=_normalize_interaction_mode(
            x_device_interaction_mode,
            default=INTERACTION_MODE_FULL_DUPLEX,
        ),
    )
