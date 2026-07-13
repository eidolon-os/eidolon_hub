"""Explicit Host-side subscriber for the Guard P1 simulation."""

from __future__ import annotations

from dataclasses import dataclass
from time import time

from eidolon_sdk.biz.guard import GuardPolicyActionAck

from hub.core.guard_policy import GuardControlPlane

MISSION_CONTROL_FIXTURE_SUBSCRIBER = "mission_control_fixture"


@dataclass(frozen=True)
class GuardFixtureDispatchResult:
    """The durable actions accepted by one explicit fixture drain."""

    acknowledged_action_ids: list[str]


class MissionControlFixtureSubscriber:
    """A deterministic Host subscriber, deliberately invoked rather than scheduled.

    It models the subscriber half of the protocol without claiming to be a UI,
    room participant, or device executor. Future production subscribers own
    their own delivery loops, authentication, retry and domain side effects.
    """

    def __init__(self, control_plane: GuardControlPlane) -> None:
        self._control_plane = control_plane

    async def drain(self, *, limit: int = 50) -> GuardFixtureDispatchResult:
        if not 1 <= limit <= 100:
            raise ValueError("fixture drain limit must be between 1 and 100")

        acknowledged: list[str] = []
        actions = await self._control_plane.pending_actions(
            subscriber=MISSION_CONTROL_FIXTURE_SUBSCRIBER
        )
        for action in actions[:limit]:
            ack = GuardPolicyActionAck(
                guard_companion_id=action["guard_companion_id"],
                device_id=action["device_id"],
                correlation_id=action["correlation_id"],
                guard_epoch=action["guard_epoch"],
                ts_ms=max(action["ts_ms"], int(time() * 1000)),
                action_id=action["action_id"],
                subscriber=MISSION_CONTROL_FIXTURE_SUBSCRIBER,
                status="completed",
            )
            await self._control_plane.handle(ack.model_dump(mode="json"))
            acknowledged.append(ack.action_id)
        return GuardFixtureDispatchResult(
            acknowledged_action_ids=acknowledged,
        )
