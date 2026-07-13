"""Transport-independent Guard fact ingress for eidolon.control data packets."""

from __future__ import annotations

import json
from typing import Any

from eidolon_sdk.biz.contracts import CONTROL_TOPIC

from hub.core.guard_policy import GuardControlPlane, GuardPolicyError, GuardPolicyResult


class GuardIngress:
    """Decode raw control-topic bytes and hand Guard messages to the control plane."""

    def __init__(self, control_plane: GuardControlPlane) -> None:
        self._control_plane = control_plane

    async def handle_packet(
        self,
        *,
        topic: str,
        data: bytes | bytearray | memoryview | str,
        sender_identity: str,
        source: str,
        require_guard: bool = False,
    ) -> GuardPolicyResult | None:
        if topic != CONTROL_TOPIC:
            if require_guard:
                raise GuardPolicyError("guard ingress only accepts eidolon.control packets")
            return None
        raw = _raw_bytes(data)
        try:
            envelope: Any = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            if require_guard:
                raise GuardPolicyError("guard ingress packet must be JSON") from exc
            return None
        if not isinstance(envelope, dict) or not str(envelope.get("type") or "").startswith("guard."):
            if require_guard:
                raise GuardPolicyError("guard ingress packet must be a Guard message")
            return None
        return await self._control_plane.handle(
            envelope,
            sender_identity=sender_identity,
            source=source,
        )


def _raw_bytes(data: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)
