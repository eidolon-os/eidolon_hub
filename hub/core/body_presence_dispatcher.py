"""Single seam that builds the body.presence.set command and sends it.

Both the durable guard body-action worker (the reflex path: guard facts ->
policy -> outbox -> worker) and the admin manual "wiggle" endpoint dispatch
through here, so the `body.presence.set` command contract lives in exactly one
place instead of being hand-built at each call site.

Device resolution (single device vs owner-scoped fan-out), capability gating,
and durability (immediate vs durable outbox) are deliberately the callers'
concern -- this only owns the command envelope payload and the send.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET


class BodyPresenceDispatcher:
    """The only component that knows the body.presence.set payload shape."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def dispatch(
        self,
        device_id: str,
        *,
        state: str,
        correlation_id: str,
        action_id: str | None = None,
        guard_epoch: int = 0,
        ttl_ms: int = 15_000,
        qos: str = "result",
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Send one body.presence.set command to a specific device.

        Raises ValueError (from the runtime) when the device is not connected;
        callers decide whether that is a 409 (manual) or a retry (durable).
        """
        payload = {
            "state": state,
            "guard_epoch": guard_epoch,
            "correlation_id": correlation_id,
            "action_id": action_id or f"body_presence_{uuid4().hex}",
        }
        return await self._runtime.send_command(
            device_id,
            payload,
            op=BODY_OP_PRESENCE_SET,
            ttl_ms=ttl_ms,
            qos=qos,
            priority=priority,
        )
