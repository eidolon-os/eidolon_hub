"""Unified LiveKit client configuration: ESP32 and web."""

from __future__ import annotations

import logging
import secrets
from enum import Enum

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from hub.api.clients import (
    AdminClient,
    AdminNotFound,
    AdminUnreachable,
    AdminUpstreamError,
)
from hub.api.routers.system.token import AgentMode, TokenResponse, generate_token
from hub.config import RuntimeAdminConfig, load_config, resolve_eidolon_livekit_client_url

_log = logging.getLogger(__name__)

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
    request: Request,
    room_name: str | None,
    device_id: str,
    agent_mode: AgentMode,
) -> ESP32ConfigResponse:
    resolved_room = room_name or f"esp32-{secrets.token_hex(4)}"
    # Phase 32.B: tag the ESP32 token with kind=device so channel knows
    # to dispatch /api/resolve/device/{id} (vs /api/resolve/user for
    # the web flow). Symmetric to _web_response's metadata.
    try:
        _identity, token = generate_token(
            room_name=resolved_room,
            participant_name=device_id,
            agent_mode=agent_mode,
            participant_metadata={"kind": "device", "device_id": device_id},
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    cfg = load_config().esp32
    try:
        server_url = resolve_eidolon_livekit_client_url(
            cfg,
            request_host=request.url.hostname or "",
            request_scheme=request.url.scheme or "http",
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return ESP32ConfigResponse(
        success=True,
        config=ESP32Config(
            server_url=server_url,
            token=token,
            identity=device_id,
            room_name=resolved_room,
            audio=AudioConfig(),
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
):
    if client_type == ClientType.ESP32:
        if not x_device_id:
            raise HTTPException(
                status_code=422,
                detail="X-Device-ID header is required when client_type=esp32",
            )
        return _esp32_response(
            request=request,
            room_name=room_name,
            device_id=x_device_id,
            agent_mode=agent_mode,
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
    )
