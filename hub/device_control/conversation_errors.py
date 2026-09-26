"""Shared authenticated device-session response boundary."""
from fastapi import HTTPException

from hub.admission.domain import AdmissionProblem
from hub.admission.http import problem_response
from hub.channel_reconciliation.domain import ChannelProviderError



async def answer(actor_provider, request, call):
    try:
        return await call(await actor_provider(request))
    except AdmissionProblem as exc:
        return problem_response(exc)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ChannelProviderError as exc:
        raise HTTPException(503 if exc.retryable else 409,
            {"code": exc.code, "retryable": exc.retryable}) from exc
