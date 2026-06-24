"""Proactive wake orchestrator — pure router (plan §3 Phase 3).

The orchestrator turns a proactive event into a room.join wake. It does NO
instance->device mapping: the publisher (agent) stamps device_id + full payload
into the event; hub only routes by device_id and skips when the device is
absent/offline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hub.config import AppConfig, LiveKitConfig
from hub.core.admin_runtime import LiveKitAdminRuntime
from hub.core.proactive_wake import ProactiveWakeOrchestrator


def _orchestrator() -> tuple[ProactiveWakeOrchestrator, LiveKitAdminRuntime]:
    cfg = AppConfig()
    cfg.livekit = LiveKitConfig(api_url="http://localhost:7880", api_key="k", api_secret="s")
    runtime = LiveKitAdminRuntime(cfg)
    runtime.send_command = AsyncMock()  # type: ignore[method-assign]
    return ProactiveWakeOrchestrator(cfg, runtime), runtime


@pytest.mark.asyncio
async def test_wake_sends_room_join_with_full_payload():
    orch, runtime = _orchestrator()
    sent = await orch.handle_event(
        {
            "device_id": "1c:db:d4:7a:ef:0c",
            "instance_id": "inst_abc",
            "intent": "long_task_done",
            "text": "会议纪要整理好了，包含摘要和行动项。",
            "style_hint": "report",
        }
    )
    assert sent is True
    runtime.send_command.assert_awaited_once()
    args, kwargs = runtime.send_command.await_args
    assert args[0] == "1c:db:d4:7a:ef:0c"          # routed by device_id
    assert kwargs["op"] == "room.join"             # wake primitive
    payload = args[1]
    # Canonical channel vocabulary — must match channel INTENT_PROACTIVE so the
    # welcome is actually suppressed (a bare "proactive" would degrade to
    # user_initiated and the device would play the greeting).
    assert payload["session_intent"] == "proactive_initiated"
    assert payload["text"] == "会议纪要整理好了，包含摘要和行动项。"
    assert payload["instance_id"] == "inst_abc"


@pytest.mark.asyncio
async def test_wake_skipped_without_device_id():
    """No device_id in the event → nothing to route (publisher's job to stamp it)."""
    orch, runtime = _orchestrator()
    sent = await orch.handle_event({"instance_id": "inst_abc", "text": "hi"})
    assert sent is False
    runtime.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_swallows_offline_device():
    """Device offline → send_command raises ValueError → skip (Phase 4 buffers)."""
    orch, runtime = _orchestrator()
    runtime.send_command = AsyncMock(side_effect=ValueError("not connected"))  # type: ignore[method-assign]
    sent = await orch.handle_event({"device_id": "dev-x", "text": "hi"})
    assert sent is False  # no raise


@pytest.mark.asyncio
async def test_wake_swallows_unexpected_error():
    orch, runtime = _orchestrator()
    runtime.send_command = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    sent = await orch.handle_event({"device_id": "dev-x", "text": "hi"})
    assert sent is False  # no raise


@pytest.mark.asyncio
async def test_disabled_start_is_noop():
    orch, _ = _orchestrator()  # cfg.proactive_wake.enabled defaults False
    await orch.start()  # must not attempt a NATS connect
    await orch.stop()
