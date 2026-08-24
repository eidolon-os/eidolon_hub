"""Authentication adapter for the canonical Admission HTTP boundary."""

from __future__ import annotations

from jose import JWTError, jwt

from hub.admission.domain import ActorContext, AdmissionProblem
from hub.contracts.bindings.admission import (
    BusinessOwnerId,
    ControllerActorRef,
    OwnerDomainId,
)


class JwtAdmissionActorProvider:
    """Verify the short-lived Admin-issued ActorContext, never body identity."""

    def __init__(
        self,
        *,
        secret: bytes,
        audience: str = "eidolon-admission",
        issuer: str | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("Admission JWT secret must contain at least 32 bytes")
        self._secret = secret
        self._audience = audience
        self._issuer = issuer

    async def __call__(self, request) -> ActorContext:
        credential = request.headers.get("Authorization", "")
        scheme, separator, token = credential.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise AdmissionProblem(
                "UNAUTHENTICATED",
                "Bearer Admission credential required",
                status=401,
                category="auth",
            )
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require_sub": True, "require_exp": True},
            )
            subject = str(claims["sub"]).strip()
            presenter = str(claims.get("presenter") or "").strip()
            if not subject or presenter != subject:
                raise ValueError("Admission presenter is not bound to the subject")
            actor = ControllerActorRef.model_validate(claims["actor"])
            owner_domain_id = OwnerDomainId(claims["owner_domain_id"])
            business_owner_id = BusinessOwnerId(claims["business_owner_id"])
            if actor.owner_domain_id != owner_domain_id:
                raise ValueError("Admission ActorRef crossed its Owner Domain")
            token_scopes = tuple(str(item) for item in claims.get("scopes", ()))
            if set(token_scopes) != set(actor.granted_scopes):
                raise ValueError("Admission token and ActorRef scopes differ")
        except (JWTError, KeyError, TypeError, ValueError) as exc:
            raise AdmissionProblem(
                "UNAUTHENTICATED",
                "invalid Admission credential",
                status=401,
                category="auth",
            ) from exc
        return ActorContext(
            actor=actor,
            owner_domain_id=owner_domain_id,
            business_owner_id=business_owner_id,
        )
