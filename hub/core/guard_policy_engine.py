"""Pure policy decisions for the Guard control plane."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_sdk.biz.guard import (
    GuardSilentPresenceConfig,
    GuardPresenceAbsent,
    GuardPresenceCandidate,
    GuardPresenceVerified,
)


@dataclass(frozen=True)
class GuardPolicyDecision:
    action: str
    payload: dict[str, str | int | float | bool | None]
    subscriber: str = "mission_control_fixture"


class GuardPolicyEngine:
    """Map Guard facts to actions without I/O or transport knowledge."""

    def decide(
        self,
        event: GuardPresenceCandidate | GuardPresenceVerified | GuardPresenceAbsent,
        *,
        policy_id: str,
        config: GuardSilentPresenceConfig,
    ) -> GuardPolicyDecision | None:
        del policy_id  # Config validation selects the single P1 policy shape.
        if isinstance(event, GuardPresenceCandidate):
            if not config.candidate_enabled:
                return None
            return GuardPolicyDecision(
                action="mission_control.annotate",
                payload={"presence": "candidate", "debounce_ms": event.debounce_ms},
            )
        if isinstance(event, GuardPresenceVerified):
            if not config.verified_enabled or event.verdict not in config.accepted_verdicts:
                return None
            return GuardPolicyDecision(
                action="mission_control.annotate",
                payload={"presence": event.verdict, "verifier": event.verifier},
            )
        if not config.absence_enabled:
            return None
        return GuardPolicyDecision(
            action="mission_control.clear",
            payload={
                "presence": "absent",
                "reason": event.reason,
                "absent_for_ms": event.absent_for_ms,
            },
        )
