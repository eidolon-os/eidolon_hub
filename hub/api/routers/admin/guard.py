"""Admin fixture endpoints for the ATK Guard P0 control-plane loop."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from eidolon_sdk.biz.contracts import CONTROL_TOPIC
from hub.core.guard_ingress import GuardIngress
from hub.core.guard_policy import GuardPolicyError

router = APIRouter(prefix="/api/admin/guard", tags=["Admin Guard"])


class GuardEventRequest(BaseModel):
    message: dict[str, Any] = Field(default_factory=dict)


class GuardEventResponse(BaseModel):
    accepted: dict[str, Any]
    action: dict[str, Any] | None = None
    actions: list[dict[str, Any]] | None = None
    topic: str


class GuardActionsResponse(BaseModel):
    actions: list[dict[str, Any]]
    topic: str = "eidolon.control"


class GuardFixtureDrainRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=100)


class GuardFixtureDrainResponse(BaseModel):
    acknowledged_action_ids: list[str]


@router.post("/events", response_model=GuardEventResponse)
async def submit_guard_event(payload: GuardEventRequest, request: Request) -> GuardEventResponse:
    """Fixture-only guard message submission.

    Device-like fake ATK tests must use ``/fake-atk/ingress`` so topic, raw
    bytes and sender identity follow the same path as LiveKit data packets.
    """
    plane = request.app.state.guard_control_plane
    try:
        result = await plane.handle(payload.message, source="fixture")
    except GuardPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return GuardEventResponse(**result.__dict__)


@router.post("/fake-atk/ingress", response_model=GuardEventResponse)
async def submit_fake_atk_packet(
    request: Request,
    sender_identity: str = Header(alias="X-Fake-Participant-Identity"),
    topic: str = Query(default=CONTROL_TOPIC),
) -> GuardEventResponse:
    ingress = getattr(request.app.state, "guard_ingress", None)
    if ingress is None:
        ingress = GuardIngress(request.app.state.guard_control_plane)
    try:
        result = await ingress.handle_packet(
            topic=topic,
            data=await request.body(),
            sender_identity=sender_identity,
            source="fake_atk",
            require_guard=True,
        )
    except GuardPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=422, detail="not a guard packet")
    return GuardEventResponse(**result.__dict__)


@router.get("/actions", response_model=GuardActionsResponse)
async def list_pending_guard_actions(request: Request) -> GuardActionsResponse:
    plane = request.app.state.guard_control_plane
    return GuardActionsResponse(actions=await plane.pending_actions())


@router.post("/fixture/drain", response_model=GuardFixtureDrainResponse)
async def drain_mission_control_fixture(
    payload: GuardFixtureDrainRequest,
    request: Request,
) -> GuardFixtureDrainResponse:
    """Explicitly execute the Host-only Mission Control fixture subscriber."""
    subscriber = request.app.state.guard_fixture_subscriber
    result = await subscriber.drain(limit=payload.limit)
    return GuardFixtureDrainResponse(**result.__dict__)
