"""Isolated RFC 9457 adapter for canonical Admission; PH2-B wires authentication."""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.admission.application import AdmissionAuthority
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.contracts.bindings.admission import DeviceRef

ActorProvider = Callable[[Request], Awaitable[ActorContext]]


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


def problem_response(problem: AdmissionProblem, *, command_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        media_type="application/problem+json",
        content={
            "type": f"https://contracts.eidolon.live/problems/{problem.code.lower()}",
            "title": problem.code,
            "status": problem.status,
            "detail": problem.detail,
            "code": problem.code,
            "category": problem.category,
            "retryable": problem.retryable,
            "authority": "admission",
            "command_id": command_id,
            "incident_id": f"incident_{uuid.uuid4().hex}",
        },
    )


def create_admission_router(
    *, authority: AdmissionAuthority, actor_provider: ActorProvider
) -> APIRouter:
    router = APIRouter(prefix="/api/admission/v1", tags=["canonical-admission"])

    @router.post("/enrollments")
    async def create(payload: dict) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            payload = dict(payload)
            payload.pop("command_id")
            correlation_id = payload.pop("correlation_id", command_id)
            return JSONResponse(
                status_code=201,
                content=await authority.create_enrollment(
                    command_id=command_id, correlation_id=correlation_id, payload=payload
                ),
            )
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}/decisions")
    async def decide(enrollment_id: str, payload: dict, request: Request) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            payload = dict(payload)
            payload.pop("command_id")
            correlation_id = payload.pop("correlation_id", command_id)
            context = await actor_provider(request)
            return JSONResponse(
                status_code=200,
                content=await authority.decide_enrollment(
                    command_id=command_id,
                    correlation_id=correlation_id,
                    enrollment_id=enrollment_id,
                    payload=payload,
                    context=context,
                ),
            )
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
            context = await actor_provider(request)
            result = await authority.cancel_enrollment(
                command_id=command_id,
                correlation_id=payload.get("correlation_id", command_id),
                enrollment_id=enrollment_id,
                reason=payload["reason"],
                context=context,
            )
            return JSONResponse(status_code=200, content=result)
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
            result = await authority.collect_claim_grant(
                command_id=command_id,
                correlation_id=payload.get("correlation_id", command_id),
                enrollment_id=enrollment_id,
                proposal_revision=payload["proposal_revision"],
                collection_challenge=payload["collection_challenge"],
                handoff_key_proof=payload["handoff_key_proof"],
            )
            return JSONResponse(status_code=200, content=result)
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/enrollments/{enrollment_id}/claim-grants/{grant_id}:ack")
    async def ack(enrollment_id: str, grant_id: str, payload: dict) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            result = await authority.ack_claim_grant(
                command_id=command_id,
                correlation_id=payload.get("correlation_id", command_id),
                enrollment_id=enrollment_id,
                grant_id=grant_id,
                operational_key_proof=payload["operational_key_proof"],
                stored_claim_generation=payload["stored_claim_generation"],
                stored_trust_epoch=payload["stored_trust_epoch"],
            )
            return JSONResponse(status_code=200, content=result)
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.post("/claims/{device_instance_id}:revoke")
    async def revoke(device_instance_id: str, payload: dict, request: Request) -> JSONResponse:
        command_id = payload.get("command_id")
        try:
            command_id = _command_id(payload)
            device_ref = DeviceRef.model_validate(payload["device_ref"])
            if device_ref.device_instance_id != device_instance_id:
                raise AdmissionProblem(
                    "INVALID_ARGUMENT",
                    "path and DeviceRef do not match",
                    status=422,
                    category="invalid",
                )
            context = await actor_provider(request)
            result = await authority.revoke_claim(
                command_id=command_id,
                correlation_id=payload.get("correlation_id", command_id),
                device_ref=device_ref,
                reason=payload["reason"],
                context=context,
            )
            return JSONResponse(status_code=200, content=result)
        except AdmissionProblem as exc:
            return problem_response(exc, command_id=command_id)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden"),
                command_id=command_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return problem_response(_invalid(exc), command_id=command_id)

    @router.get("/claims/{device_instance_id}")
    async def get_claim(device_instance_id: str, request: Request) -> JSONResponse:
        try:
            context = await actor_provider(request)
            claim = await authority.get_claim(
                device_instance_id=device_instance_id, context=context
            )
            return JSONResponse(status_code=200, content=claim.model_dump(mode="json"))
        except AdmissionProblem as exc:
            return problem_response(exc)
        except PermissionError as exc:
            return problem_response(
                AdmissionProblem("FORBIDDEN", str(exc), status=403, category="forbidden")
            )

    return router
