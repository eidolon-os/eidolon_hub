"""Pure PH2 Admission states, identity context and stable problems."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import rfc8785

from hub.contracts.bindings.admission import (
    BusinessOwnerId,
    ClaimState,
    ControllerActorRef,
    EnrollmentProposalState,
    OwnerDomainId,
)


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor: ControllerActorRef
    owner_domain_id: OwnerDomainId
    business_owner_id: BusinessOwnerId

    def __post_init__(self) -> None:
        if self.actor.owner_domain_id != self.owner_domain_id:
            raise ValueError("ActorContext Owner Domain does not match ActorRef")

    def require_scope(self, scope: str) -> None:
        if scope not in self.actor.granted_scopes:
            raise AdmissionProblem(
                "FORBIDDEN",
                f"Controller ActorRef lacks required scope {scope}",
                status=403,
                category="forbidden",
            )


class AdmissionProblem(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        category: str = "conflict",
        retryable: bool = False,
        status: int = 409,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.category = category
        self.retryable = retryable
        self.status = status


TERMINAL_PROPOSAL_STATES = frozenset(
    {
        EnrollmentProposalState.GRANT_ACKNOWLEDGED.value,
        EnrollmentProposalState.REJECTED.value,
        EnrollmentProposalState.EXPIRED.value,
        EnrollmentProposalState.CANCELED.value,
        EnrollmentProposalState.CLAIM_REVOKED.value,
    }
)

ALLOWED_PROPOSAL_TRANSITIONS = {
    EnrollmentProposalState.PENDING_REVIEW.value: frozenset(
        {
            EnrollmentProposalState.APPROVED_AWAITING_HANDOFF.value,
            EnrollmentProposalState.REJECTED.value,
            EnrollmentProposalState.EXPIRED.value,
            EnrollmentProposalState.CANCELED.value,
        }
    ),
    EnrollmentProposalState.APPROVED_AWAITING_HANDOFF.value: frozenset(
        {
            EnrollmentProposalState.GRANT_DELIVERED.value,
            EnrollmentProposalState.EXPIRED.value,
            EnrollmentProposalState.CLAIM_REVOKED.value,
        }
    ),
    EnrollmentProposalState.GRANT_DELIVERED.value: frozenset(
        {
            EnrollmentProposalState.GRANT_ACKNOWLEDGED.value,
            EnrollmentProposalState.EXPIRED.value,
            EnrollmentProposalState.CLAIM_REVOKED.value,
        }
    ),
}

ALLOWED_CLAIM_TRANSITIONS = {
    ClaimState.ACTIVE.value: frozenset({ClaimState.SUSPENDED.value, ClaimState.REVOKED.value}),
    ClaimState.SUSPENDED.value: frozenset({ClaimState.ACTIVE.value, ClaimState.REVOKED.value}),
}


def require_transition(current: str, target: str) -> None:
    if target not in ALLOWED_PROPOSAL_TRANSITIONS.get(current, frozenset()):
        raise AdmissionProblem(
            "PROPOSAL_TERMINAL" if current in TERMINAL_PROPOSAL_STATES else "REVISION_CONFLICT",
            f"proposal transition {current} -> {target} is not allowed",
        )


def require_claim_transition(current: str, target: str) -> None:
    if target not in ALLOWED_CLAIM_TRANSITIONS.get(current, frozenset()):
        raise AdmissionProblem(
            "CLAIM_REVOKED" if current == ClaimState.REVOKED.value else "REVISION_CONFLICT",
            f"Claim transition {current} -> {target} is not allowed",
        )


def fingerprint(command_type: str, owner_domain_id: OwnerDomainId, payload: Any) -> str:
    document = {
        "command_type": command_type,
        "owner_domain_id": str(owner_domain_id),
        "payload": payload,
    }
    return "sha256:" + hashlib.sha256(rfc8785.dumps(document)).hexdigest()
