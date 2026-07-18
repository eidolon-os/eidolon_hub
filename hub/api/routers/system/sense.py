"""Internal sense.* fact ingress for the host Vision Worker (owner-scoped, D1 1A').

The Vision Worker (``eidolon_vision``) posts distilled ``sense.fatigue`` /
``sense.event`` facts here. Owner-scoped: the fact names its observing device +
owner; the Hub validates the device belongs to that owner (general device
model, NOT a GuardBinding) and audits it. Internal / network-restricted; image
bytes never reach the Hub — only the bounded, non-sensitive fact does.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from hub.core.sense_ingress import SenseIngress, SenseIngressError

router = APIRouter(prefix="/internal/sense", tags=["Sense"])


@router.post("/facts", status_code=202)
async def submit_sense_fact(request: Request) -> dict:
    store = getattr(request.app.state, "data_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="data store unavailable")
    payload = await request.json()
    try:
        accepted = await SenseIngress(store).ingest(payload, source="vision_worker")
    except SenseIngressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # includes pydantic ValidationError
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accepted": accepted}
