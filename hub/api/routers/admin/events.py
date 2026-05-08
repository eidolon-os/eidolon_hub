from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from hub.api.routers.admin.schemas import MetricsResponse, ProbeHealthResponse

router = APIRouter(prefix="/api/admin", tags=["Admin Events"])


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
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: message\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            runtime.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(request: Request):
    runtime = request.app.state.admin_runtime
    return MetricsResponse(**(await runtime.get_metrics()))
