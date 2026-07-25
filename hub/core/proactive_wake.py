"""Proactive wake orchestrator (plan §3 Phase 3).

Subscribes to the agent's proactive-trigger events on NATS and wakes the target
device by sending a ``room.join`` control command (which pulls an idle device
from its control room into a voice room — the existing ESP32 wake primitive).

Pure router: the publisher (agent ``mementos`` worker) stamps ``device_id`` plus
the full payload (``intent``/``text``/``instance_id``/``style_hint``) into the
event, so hub does NO instance->device mapping — it only routes by the
already-resolved ``device_id``. A device that's offline / not currently
connected is logged and skipped (durable buffering is Phase 4).
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from eidolon_sdk.biz.contracts import (
    CONTROL_OP_ROOM_JOIN,
    SESSION_INTENT_FIELD,
    SESSION_INTENT_PROACTIVE,
)

from hub.config import AppConfig
from hub.core.admin_runtime import LiveKitAdminRuntime

logger = logging.getLogger(__name__)

# Every wake the orchestrator sends is a proactive session: the command carries
# SESSION_INTENT_PROACTIVE verbatim → device forwards it as the
# X-Device-Session-Intent header → hub stamps it into the voice token → channel
# suppresses the welcome + runs the proactive short-window.


class ProactiveWakeOrchestrator:
    """NATS subscriber that turns a proactive event into a room.join wake."""

    def __init__(self, config: AppConfig, runtime: LiveKitAdminRuntime) -> None:
        self._cfg = config.proactive_wake
        self._runtime = runtime
        self._nc: Any | None = None
        self._sub: Any | None = None
        self._started = False

    async def start(self) -> None:
        if not self._cfg.enabled:
            logger.info("proactive wake orchestrator disabled (proactive_wake.enabled=false)")
            return
        try:
            import nats  # noqa: PLC0415 — optional dependency, loaded lazily
        except Exception as exc:  # pragma: no cover - import guard
            logger.warning("proactive wake disabled: nats client unavailable (%s)", exc)
            return
        try:
            self._nc = await nats.connect(
                self._cfg.nats_url,
                name="eidolon-hub-proactive",
                connect_timeout=5,
                max_reconnect_attempts=-1,
            )
        except Exception:
            logger.exception(
                "proactive wake: NATS connect failed url=%s (orchestrator off)",
                self._cfg.nats_url,
            )
            return
        self._sub = await self._nc.subscribe(self._cfg.wake_subject, cb=self._on_message)
        self._started = True
        logger.info(
            "proactive wake orchestrator subscribed subject=%s url=%s",
            self._cfg.wake_subject,
            self._cfg.nats_url,
        )

    async def stop(self) -> None:
        self._started = False
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.drain()
        self._nc = None
        self._sub = None

    async def _on_message(self, msg: Any) -> None:
        try:
            payload = json.loads(msg.data.decode()) if getattr(msg, "data", None) else {}
        except Exception:
            logger.debug(
                "proactive wake: malformed event JSON subject=%s",
                getattr(msg, "subject", "?"),
                exc_info=True,
            )
            return
        await self.handle_event(payload if isinstance(payload, dict) else {})

    async def handle_event(self, payload: dict[str, Any]) -> bool:
        """Route one proactive event to a room.join wake.

        Returns True iff a wake command was sent. Pure routing — no mapping; the
        device_id must already be in the event (publisher's job).
        """
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            logger.info(
                "proactive wake skipped: event has no device_id (instance_id=%s)",
                payload.get("instance_id"),
            )
            return False

        text = str(payload.get("text") or "")
        command_payload = {
            SESSION_INTENT_FIELD: SESSION_INTENT_PROACTIVE,
            "text": text,
            "instance_id": payload.get("instance_id"),
            "style_hint": payload.get("style_hint"),
        }
        try:
            await self._runtime.send_command(
                device_id,
                command_payload,
                op=CONTROL_OP_ROOM_JOIN,
                ttl_ms=self._cfg.command_ttl_ms,
            )
        except ValueError as exc:
            # Device offline / not connected. Phase 4 will buffer + replay on
            # next presence-online; for now we log and drop.
            logger.info(
                "proactive wake: device %s not reachable (%s); dropping "
                "(durable buffer is Phase 4)",
                device_id,
                exc,
            )
            return False
        except Exception:
            logger.exception(
                "proactive wake: send_command(room.join) failed device=%s", device_id
            )
            return False
        logger.info(
            "proactive wake: room.join sent device=%s session_intent=%s chars=%d",
            device_id,
            SESSION_INTENT_PROACTIVE,
            len(text),
        )
        return True
