"""Owner Domain discovery; Admission owns every enrollment mutation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter

from hub.contracts.bindings.onboarding import OwnerDomainDescriptor


@dataclass(frozen=True, slots=True)
class DeviceOnboardingHttpServices:
    descriptor: OwnerDomainDescriptor


def create_device_onboarding_router(
    services: DeviceOnboardingHttpServices | Callable[[], DeviceOnboardingHttpServices],
) -> APIRouter:
    router = APIRouter(prefix="/api/device-onboarding/v1", tags=["device-onboarding"])

    def current() -> DeviceOnboardingHttpServices:
        return services() if callable(services) else services

    @router.get("/descriptor", response_model=OwnerDomainDescriptor)
    async def descriptor() -> OwnerDomainDescriptor:
        return current().descriptor

    return router
