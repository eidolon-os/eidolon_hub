from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from eidolon_sdk.core.streaming import encode_sse_event
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from hub.api.routers.admin.schemas import MetricsResponse, ProbeHealthResponse

router = APIRouter(prefix="/api/admin", tags=["Admin Events"])
_SSE_PING_INTERVAL_SECONDS = 15.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_runtime_event(payload: str) -> dict:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {"type": "raw", "payload": payload}
    if isinstance(value, dict):
        return value
    return {"type": "raw", "payload": value}


@router.get("/probe/health", response_model=ProbeHealthResponse)
async def get_probe_health(request: Request):
    runtime = request.app.state.admin_runtime
    health = runtime.get_probe_health()
    return ProbeHealthResponse(
        running=health.running,
        last_success_at=health.last_success_at,
        last_error=health.last_error,
        consecutive_failures=health.consecutive_failures,
        total_cycles=health.total_cycles,
    )


@router.get("/stream/events")
async def stream_events(request: Request):
    runtime = request.app.state.admin_runtime
    queue = await runtime.subscribe()

    async def event_generator():
        yield encode_sse_event(
            "connected",
            {
                "type": "connected",
                "at": _utc_now(),
                "scope": "hub.admin_runtime",
                "replay": False,
                "sources": ["probe_cycle", "command_updated"],
            },
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=_SSE_PING_INTERVAL_SECONDS,
                    )
                    yield encode_sse_event("hub_event", _decode_runtime_event(payload))
                except asyncio.TimeoutError:
                    yield encode_sse_event("ping", {"type": "ping", "at": _utc_now()})
        finally:
            runtime.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(request: Request):
    runtime = request.app.state.admin_runtime
    return MetricsResponse(**(await runtime.get_metrics()))
