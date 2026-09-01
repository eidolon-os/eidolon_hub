"""Isolated RFC 9457 adapter for canonical Admission; PH2-B wires authentication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.admission.application import AdmissionAuthority
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.contracts.bindings.admission import (
    AckClaimGrant,
    AckClaimGrantResult,
    AdmissionListCursor,
    CancelEnrollment,
    CancelEnrollmentResult,
    ClaimEventCursor,
    ClaimQuery,
    ClaimState,
    CollectClaimGrant,
    CollectClaimGrantResult,
    CreateEnrollment,
    CreateEnrollmentResult,
    DecideEnrollment,
    DecideEnrollmentResult,
    DeviceProblem,
    EnrollmentProposalQuery,
    EnrollmentProposalState,
    OwnerDomainId,
    RevokeClaim,
    RevokeClaimResult,
)

ActorProvider = Callable[[Request], Awaitable[ActorContext]]
ClaimEventReaderProvider = Callable[[Request], Awaitable[OwnerDomainId]]


def _invalid(exc: Exception) -> AdmissionProblem:
    return AdmissionProblem(
        "INVALID_ARGUMENT",
        str(exc) or "request is invalid",
        status=422,
        category="invalid",
    )


def _command_id(payload: dict) -> str:
    value = payload.get("command_id")
    if not isinstance(value, str) or not value.strip():
        raise AdmissionProblem(
            "INVALID_ARGUMENT", "command_id is required", status=422, category="invalid"
        )
    return value


def _strict(payload: dict, allowed: set[str]) -> dict:
    unknown = set(payload) - allowed
    missing = allowed - set(payload)
    if unknown or missing:
        raise AdmissionProblem(
            "INVALID_ARGUMENT",
            f"canonical request fields differ: missing={sorted(missing)}, unknown={sorted(unknown)}",
            status=422,
            category="invalid",
        )
    return payload


def problem_response(problem: AdmissionProblem, *, command_id: str | None = None) -> JSONResponse:
    values = {
        "code": problem.code,
        "category": problem.category,
        "retryable": problem.retryable,
        "authority": "admission",
        "command_id": command_id,
        "resource_ref": None,
        "current_revision": None,
        "current_generation": None,
        "retry_after_ms": None,
        "detail": problem.detail,
        "incident_id": f"incident_{uuid.uuid4().hex}",
    }
    try:
        canonical = DeviceProblem.model_validate(values)
    except ValueError:
        values["command_id"] = None
        canonical = DeviceProblem.model_validate(values)
    return JSONResponse(
        status_code=problem.status,
        media_type="application/problem+json",
        content=canonical.model_dump(mode="json"),
    )


def create_admission_router(
    *,
    authority: AdmissionAuthority | Callable[[], AdmissionAuthority],
    actor_provider: ActorProvider,
    claim_event_reader_provider: ClaimEventReaderProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/admission/v1", tags=["canonical-admission"])

    def current() -> AdmissionAuthority:
        return authority() if callable(authority) else authority

    async def claim_event_owner(request: Request) -> OwnerDomainId:
        if claim_event_reader_provider is not None:
            return await claim_event_reader_provider(request)
        context = await actor_provider(request)
        context.require_scope("device.claim.events.read")
        return context.owner_domain_id

    @router.post("/enrollments")
    async def create(payload: dict) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, {"command_id", "correlation_id", *CreateEnrollment.model_fields})
            correlation_id = payload["correlation_id"]
            command = CreateEnrollment.model_validate(
                {
                    key: value
                    for key, value in payload.items()
                    if key in CreateEnrollment.model_fields
                }
            )
            result = await current().create_enrollment(
                command_id=command_id,
                correlation_id=correlation_id,
                payload=command.model_dump(mode="json"),
            )
            canonical = CreateEnrollmentResult(
                enrollment_id=result["enrollment_id"],
                proposal_revision=result["proposal_revision"],
                state=result["state"],
                expires_at=result["expires_at"],
                reviewed_manifest_digest=result["reviewed_manifest_ref"]["digest"],
                collection_challenge=result["collection_challenge"],
            )
            return JSONResponse(status_code=201, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}/decisions")
    async def decide(enrollment_id: str, payload: dict, request: Request) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, {"command_id", "correlation_id", *DecideEnrollment.model_fields})
            command = DecideEnrollment.model_validate(
                {
                    key: value
                    for key, value in payload.items()
                    if key in DecideEnrollment.model_fields
                }
            )
            if command.enrollment_id != enrollment_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and command enrollment_id differ",
                    status=422,
                    category="invalid",
                )
            context = await actor_provider(request)
            result = await current().decide_enrollment(
                command_id=command_id,
                correlation_id=payload["correlation_id"],
                enrollment_id=enrollment_id,
                payload=command.model_dump(mode="json", exclude={"enrollment_id"}),
                context=context,
            )
            decision = result["decision"]
            canonical = DecideEnrollmentResult(
                decision_id=decision["decision_id"],
                decision=decision["decision"],
                decided_by=decision["actor"],
                decided_at=decision["decided_at"],
                proposal_revision=result["proposal_revision"],
            )
            return JSONResponse(status_code=200, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden"),
                command_id=command_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}:cancel")
    async def cancel(enrollment_id: str, payload: dict, request: Request) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, {"command_id", "correlation_id", *CancelEnrollment.model_fields})
            command = CancelEnrollment.model_validate(
                {
                    key: value
                    for key, value in payload.items()
                    if key in CancelEnrollment.model_fields
                }
            )
            if command.enrollment_id != enrollment_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and cancel command enrollment_id differ",
                    status=422,
                    category="invalid",
                )
            context = await actor_provider(request)
            result = await current().cancel_enrollment(
                command_id=command_id,
                correlation_id=payload["correlation_id"],
                enrollment_id=enrollment_id,
                reason=command.reason,
                context=context,
            )
            canonical = CancelEnrollmentResult(
                enrollment_id=result["enrollment_id"],
                proposal_state=result["proposal_state"],
                canceled_at=result["occurred_at"],
            )
            return JSONResponse(status_code=200, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden"),
                command_id=command_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}/claim-grants:collect")
    async def collect(enrollment_id: str, payload: dict) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, {"command_id", "correlation_id", *CollectClaimGrant.model_fields})
            command = CollectClaimGrant.model_validate(
                {
                    key: value
                    for key, value in payload.items()
                    if key in CollectClaimGrant.model_fields
                }
            )
            if command.enrollment_id != enrollment_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and command enrollment_id differ",
                    status=422,
                    category="invalid",
                )
            result = await current().collect_claim_grant(
                command_id=command_id,
                correlation_id=payload["correlation_id"],
                enrollment_id=enrollment_id,
                proposal_revision=command.proposal_revision,
                collection_challenge=command.collection_challenge,
                handoff_key_proof=command.handoff_key_proof,
            )
            canonical = CollectClaimGrantResult.model_validate(
                {key: result[key] for key in CollectClaimGrantResult.model_fields}
            )
            return JSONResponse(status_code=200, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}/claim-grants/{grant_id}:ack")
    async def ack(enrollment_id: str, grant_id: str, payload: dict) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, {"command_id", "correlation_id", *AckClaimGrant.model_fields})
            command = AckClaimGrant.model_validate(
                {key: value for key, value in payload.items() if key in AckClaimGrant.model_fields}
            )
            if command.enrollment_id != enrollment_id or command.grant_id != grant_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and Ack command identity differ",
                    status=422,
                    category="invalid",
                )
            result = await current().ack_claim_grant(
                command_id=command_id,
                correlation_id=payload["correlation_id"],
                enrollment_id=enrollment_id,
                grant_id=grant_id,
                operational_key_proof=command.operational_key_proof,
                stored_claim_generation=command.stored_claim_generation,
                stored_trust_epoch=command.stored_trust_epoch,
            )
            canonical = AckClaimGrantResult.model_validate(
                {key: result[key] for key in AckClaimGrantResult.model_fields}
            )
            return JSONResponse(status_code=200, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/claims/{device_instance_id}:revoke")
    async def revoke(device_instance_id: str, payload: dict, request: Request) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            _strict(payload, set(RevokeClaim.model_fields))
            command = RevokeClaim.model_validate(payload)
            device_ref = command.device_ref
            if device_ref.device_instance_id != device_instance_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and DeviceRef do not match",
                    status=422,
                    category="invalid",
                )
            context = await actor_provider(request)
            result = await current().revoke_claim(
                command_id=command_id,
                correlation_id=command.correlation_id,
                device_ref=device_ref,
                reason=command.reason,
                context=context,
            )
            canonical = RevokeClaimResult.model_validate(
                {key: result[key] for key in RevokeClaimResult.model_fields}
            )
            return JSONResponse(status_code=200, content=canonical.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden"),
                command_id=command_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.get("/base-identities/{device_base_id}")
    async def describe_base_identity(
        device_base_id: str, request: Request, operational_key_id: str
    ) -> JSONResponse:
        try:
            context = await actor_provider(request)
            described = await current().describe_base_identity(
                device_base_id=device_base_id,
                operational_key_id=operational_key_id,
                context=context,
            )
            return JSONResponse(status_code=200, content=described)
        except AdmissionProblem as exc:
            return problem_response(exc)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden")
            )

    @router.get("/claims/{device_instance_id}")
    async def get_claim(device_instance_id: str, request: Request) -> JSONResponse:
        try:
            context = await actor_provider(request)
            claim = await current().get_claim(
                device_instance_id=device_instance_id, context=context
            )
            return JSONResponse(status_code=200, content=claim.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden")
            )

    @router.get("/enrollments/{enrollment_id}")
    async def get_enrollment(enrollment_id: str, request: Request) -> JSONResponse:
        try:
            context = await actor_provider(request)
            projection = await current().get_enrollment_recovery(
                enrollment_id=enrollment_id, context=context
            )
            return JSONResponse(status_code=200, content=projection.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc)

    @router.get("/enrollments")
    async def list_enrollments(
        request: Request,
        states: str = "pending_review,approved_awaiting_handoff,grant_delivered,grant_acknowledged",
        limit: int = 50,
        after_sort_key: datetime | None = None,
        after_resource_id: str | None = None,
    ) -> JSONResponse:
        try:
            context = await actor_provider(request)
            if (after_sort_key is None) != (after_resource_id is None):
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "both cursor fields are required",
                    status=422,
                    category="invalid",
                )
            cursor = (
                AdmissionListCursor(
                    owner_domain_id=context.owner_domain_id,
                    sort_key=after_sort_key,
                    resource_id=after_resource_id,
                )
                if after_sort_key is not None and after_resource_id is not None
                else None
            )
            query = EnrollmentProposalQuery(
                owner_domain_id=context.owner_domain_id,
                states=tuple(EnrollmentProposalState(item) for item in states.split(",") if item),
                cursor=cursor,
                limit=limit,
            )
            page = await current().list_enrollment_recovery(query=query, context=context)
            return JSONResponse(status_code=200, content=page.model_dump(mode="json"))
        except (AdmissionProblem, ValueError) as exc:
            return problem_response(exc if isinstance(exc, AdmissionProblem) else _invalid(exc))

    @router.get("/claims")
    async def list_claims(
        request: Request,
        states: str = "active,suspended,revoked",
        limit: int = 50,
        after_sort_key: datetime | None = None,
        after_resource_id: str | None = None,
    ) -> JSONResponse:
        try:
            context = await actor_provider(request)
            if (after_sort_key is None) != (after_resource_id is None):
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "both cursor fields are required",
                    status=422,
                    category="invalid",
                )
            cursor = (
                AdmissionListCursor(
                    owner_domain_id=context.owner_domain_id,
                    sort_key=after_sort_key,
                    resource_id=after_resource_id,
                )
                if after_sort_key is not None and after_resource_id is not None
                else None
            )
            query = ClaimQuery(
                owner_domain_id=context.owner_domain_id,
                states=tuple(ClaimState(item) for item in states.split(",") if item),
                cursor=cursor,
                limit=limit,
            )
            page = await current().list_claims(query=query, context=context)
            return JSONResponse(status_code=200, content=page.model_dump(mode="json"))
        except (AdmissionProblem, ValueError) as exc:
            return problem_response(exc if isinstance(exc, AdmissionProblem) else _invalid(exc))

    @router.get("/claim-events")
    async def claim_events(
        request: Request, after_stream_position: int = 0, limit: int = 100
    ) -> JSONResponse:
        try:
            owner_domain_id = await claim_event_owner(request)
            if not 1 <= limit <= 500:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "claim event page limit must be between 1 and 500",
                    status=422,
                    category="invalid",
                )
            page = await current().claim_event_page(
                cursor=ClaimEventCursor(stream_position=after_stream_position),
                limit=limit,
                owner_domain_id=owner_domain_id,
            )
            return JSONResponse(status_code=200, content=page.model_dump(mode="json"))
        except (AdmissionProblem, ValueError) as exc:
            return problem_response(exc if isinstance(exc, AdmissionProblem) else _invalid(exc))

    return router
