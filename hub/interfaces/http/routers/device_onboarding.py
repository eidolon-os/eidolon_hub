"""HTTPS interface for short-lived device enrollment and Provider handoff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException, Response, status

from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.handoff_device import HandoffDevice
from hub.contracts.bindings.onboarding import (
    DeviceEnrollment,
    DeviceEnrollmentReceipt,
    DeviceHandoffOutcome,
    DeviceHandoffRequest,
    HubDescriptor,
)
from hub.contracts.mappers import (
    enrollment_receipt_to_wire,
    enrollment_to_domain,
    handoff_outcome_to_wire,
)
from hub.domain.devices.entities import DeviceLifecycleState
from hub.ports.channels import ChannelProviderContractError


@dataclass(frozen=True, slots=True)
class DeviceOnboardingHttpServices:
    descriptor: HubDescriptor
    enroll: EnrollDevice
    handoff: HandoffDevice


def create_device_onboarding_router(
    services: DeviceOnboardingHttpServices | Callable[[], DeviceOnboardingHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/device-onboarding/v1", tags=["device-onboarding"])

    def current() -> DeviceOnboardingHttpServices:
        return services() if callable(services) else services

    @router.get("/descriptor", response_model=HubDescriptor)
    async def descriptor() -> HubDescriptor:
        return current().descriptor

    @router.post("/enrollments", response_model=DeviceEnrollmentReceipt)
    async def enroll(payload: DeviceEnrollment) -> DeviceEnrollmentReceipt:
        try:
            device = await current().enroll.execute(enrollment_to_domain(payload))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return enrollment_receipt_to_wire(device, request_id=payload.request_id)

    @router.post(
        "/enrollments/{enrollment_id}/handoff",
        response_model=DeviceHandoffOutcome,
    )
    async def handoff(
        enrollment_id: str,
        payload: DeviceHandoffRequest,
        response: Response,
    ) -> DeviceHandoffOutcome:
        try:
            outcome = await current().handoff.execute(
                enrollment_id=enrollment_id,
                retrieval_token=payload.retrieval_token,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="enrollment not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except ChannelProviderContractError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if outcome.device.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL:
            response.status_code = status.HTTP_202_ACCEPTED
        return handoff_outcome_to_wire(
            outcome.device,
            outcome.assignments,
            request_id=payload.request_id,
        )

    return router
