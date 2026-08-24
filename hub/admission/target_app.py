"""Explicit PH2-B0 consumer target; never imported by the default Pi5 composition."""

from __future__ import annotations

from fastapi import FastAPI

from hub.admission.application import AdmissionAuthority
from hub.admission.http import ActorProvider, create_admission_router


def create_admission_target_app(
    *, authority: AdmissionAuthority, actor_provider: ActorProvider
) -> FastAPI:
    """Compose only the canonical Admission surface for contract/process probes."""

    app = FastAPI(
        title="Eidolon Canonical Admission Consumer Target",
        version="1.0.0-ph2b0",
    )
    app.include_router(create_admission_router(authority=authority, actor_provider=actor_provider))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "traffic": "consumer-target-only"}

    return app
