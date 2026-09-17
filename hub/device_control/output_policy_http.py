"""Output policy uses the existing short-lived scoped Controller credential."""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from hub.admission.domain import AdmissionProblem
from hub.admission.domain import ActorContext
from hub.admission.http import ActorProvider, problem_response
from hub.contracts.bindings.presentation import (
    DeviceOutputConfiguration,
    DeviceOutputPolicy,
    ReadDeviceOutputPolicy,
)
from hub.ports.repositories import ConcurrentDeviceMutationError

from .output_policy import (
    OutputPolicyConflict,
    ReadDeviceOutputConfiguration,
    SetOutputPolicy,
    UpdateDeviceOutputPolicy,
)


def create_output_policy_router(
    *,
    service: Callable[[], UpdateDeviceOutputPolicy],
    reader: Callable[[], ReadDeviceOutputConfiguration],
    actor_provider: ActorProvider,
) -> APIRouter:
    router = APIRouter(prefix="/api/device-control/v1", tags=["device-control"])

    async def answer(request: Request, ask: Callable[[ActorContext], Awaitable[object]]):
        """One resource, one set of refusals, whichever verb asked."""

        try:
            return await ask(await actor_provider(request))
        except AdmissionProblem as exc:
            return problem_response(exc)
        except KeyError as exc:
            raise HTTPException(404, "device not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (OutputPolicyConflict, ConcurrentDeviceMutationError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.put("/output-policy", response_model=DeviceOutputPolicy, status_code=202)
    async def update(payload: SetOutputPolicy, request: Request):
        return await answer(
            request, lambda context: service().execute(command=payload, context=context)
        )

    # A query rather than a GET: a DeviceRef is five values that fence one
    # another, and flattening it into a path or a query string is how a read
    # ends up answering about a generation nobody asked about.
    @router.post("/output-policy-queries", response_model=DeviceOutputConfiguration)
    async def read(payload: ReadDeviceOutputPolicy, request: Request):
        return await answer(
            request, lambda context: reader().execute(query=payload, context=context)
        )

    return router
