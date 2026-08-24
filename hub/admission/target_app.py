"""Explicit PH2-B0 consumer target; never imported by the default Pi5 composition."""

from __future__ import annotations

from fastapi import FastAPI

from hub.admission.application import AdmissionAuthority
from hub.admission.http import (
    ActorProvider,
    ClaimEventReaderProvider,
    create_admission_router,
)


def create_admission_target_app(
    *,
    authority: AdmissionAuthority,
    actor_provider: ActorProvider,
    claim_event_reader_provider: ClaimEventReaderProvider | None = None,
) -> FastAPI:
    """Compose only the canonical Admission surface for contract/process probes.

    A consumer probe has to be able to authenticate the way the consumer does.
    Kernel reads the Claim event stream as a workload with an exact capability,
    not as a Controller ActorRef, so a probe that could only offer the Controller
    path would exercise a route no deployment uses — and would keep passing while
    the path Kernel actually takes drifted.
    """

    app = FastAPI(
        title="Eidolon Canonical Admission Consumer Target",
        version="1.0.0-ph2b0",
    )
    app.include_router(
        create_admission_router(
            authority=authority,
            actor_provider=actor_provider,
            claim_event_reader_provider=claim_event_reader_provider,
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "traffic": "consumer-target-only"}

    return app
