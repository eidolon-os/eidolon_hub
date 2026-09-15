"""Output policy uses the existing short-lived scoped Controller credential."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request

from hub.admission.domain import AdmissionProblem
from hub.admission.http import ActorProvider, problem_response
from hub.contracts.bindings.presentation import DeviceOutputPolicy
from hub.ports.repositories import ConcurrentDeviceMutationError

from .output_policy import OutputPolicyConflict, SetOutputPolicy, UpdateDeviceOutputPolicy


def create_output_policy_router(
    *, service: Callable[[], UpdateDeviceOutputPolicy], actor_provider: ActorProvider
) -> APIRouter:
    router = APIRouter(prefix="/api/device-control/v1", tags=["device-control"])

    @router.put("/output-policy", response_model=DeviceOutputPolicy, status_code=202)
    async def update(payload: SetOutputPolicy, request: Request):
        try:
            context = await actor_provider(request)
            return await service().execute(command=payload, context=context)
        except AdmissionProblem as exc:
            return problem_response(exc)
        except KeyError as exc:
            raise HTTPException(404, "device not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (OutputPolicyConflict, ConcurrentDeviceMutationError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
