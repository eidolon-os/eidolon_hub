"""Authentication adapter for the canonical Admission HTTP boundary."""

from __future__ import annotations

from hub.admission.domain import ActorContext, AdmissionProblem
from hub.contracts.bindings.admission import (
    AdmissionCredentialError,
    read_admission_credential,
)


class JwtAdmissionActorProvider:
    """Verify the short-lived Admin-issued ActorContext, never body identity.

    What a credential carries is not decided here. It is one definition in the
    SDK, which Admin mints from and this reads with — so there is no second
    list of claim names to keep in step.

    There was, and they diverged: the removal path minted a different Hub
    surface's vocabulary, this side took ``claims["actor"]``, raised KeyError
    inside a broad ``except``, and answered 401 "invalid Admission credential".
    Every device removal anyone attempted was refused by that, and the refusal
    never mentioned a credential. Reading with a shared function is what makes
    that particular disagreement impossible rather than merely tested for.
    """

    def __init__(self, *, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("Admission JWT secret must contain at least 32 bytes")
        self._secret = secret

    async def __call__(self, request) -> ActorContext:
        try:
            credential = read_admission_credential(
                request.headers.get("Authorization", ""), secret=self._secret
            )
        except AdmissionCredentialError as exc:
            raise AdmissionProblem(
                "UNAUTHENTICATED",
                str(exc),
                status=401,
                category="auth",
            ) from exc
        return ActorContext(
            actor=credential.actor,
            owner_domain_id=credential.owner_domain_id,
            business_owner_id=credential.business_owner_id,
        )
