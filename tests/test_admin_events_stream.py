from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hub.api.routers.admin import events as events_router
from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime


class _Request:
    def __init__(self, runtime: LiveKitAdminRuntime):
        self.app = SimpleNamespace(state=SimpleNamespace(admin_runtime=runtime))

    async def is_disconnected(self) -> bool:
        return False


async def _close_iterator(iterator) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


@pytest.mark.asyncio
async def test_events_stream_emits_connected_then_hub_events():
    runtime = LiveKitAdminRuntime(AppConfig())
    response = await events_router.stream_events(_Request(runtime))  # type: ignore[arg-type]
    iterator = response.body_iterator

    try:
        first = await anext(iterator)
        assert b"event: connected\n" in first
        assert b'"scope": "hub.admin_runtime"' in first

        await runtime._emit_event({"type": "probe_cycle", "detected": 1, "ignored": 0})
        second = await asyncio.wait_for(anext(iterator), timeout=1)
        assert b"event: hub_event\n" in second
        assert b'"type": "probe_cycle"' in second
        assert b'"detected": 1' in second
    finally:
        await _close_iterator(iterator)

    assert len(runtime._subscribers) == 0


@pytest.mark.asyncio
async def test_events_stream_emits_ping_when_idle(monkeypatch):
    monkeypatch.setattr(events_router, "_SSE_PING_INTERVAL_SECONDS", 0.01)
    runtime = LiveKitAdminRuntime(AppConfig())
    response = await events_router.stream_events(_Request(runtime))  # type: ignore[arg-type]
    iterator = response.body_iterator

    try:
        await anext(iterator)
        second = await asyncio.wait_for(anext(iterator), timeout=1)
        assert b"event: ping\n" in second
        assert b'"type": "ping"' in second
    finally:
        await _close_iterator(iterator)
