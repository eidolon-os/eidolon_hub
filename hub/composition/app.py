"""Production composition root for Hub device onboarding and management."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hub.admission.application import AdmissionAuthority
from hub.admission.auth import JwtAdmissionActorProvider
from hub.admission.domain import AdmissionProblem
from hub.admission.http import create_admission_router
from hub.admission.persistence import SqlAdmissionStore
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.channel_reconciliation.application import (
    PeriodicChannelRevocationReconcile,
    ReconcileChannelBinding,
    ReconcileChannelRevocations,
)
from hub.channel_reconciliation.provider_client import ChannelProviderHttpClient
from hub.composition.device_onboarding import build_device_onboarding
from hub.composition.management import build_device_management
from hub.composition.resources import (
    load_runtime_secrets,
    open_runtime_resources,
)
from hub.config import HubConfig, load_hub_config
from hub.device_control.application import (
    AcknowledgeDeviceEraseOperation,
    PeriodicDeviceEraseReconcile,
    PullDeviceConfiguration,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from hub.device_control.http import (
    DeviceEraseHttpServices,
    create_device_erase_router,
)
from hub.interfaces.http.routers.device_management import (
    DeviceManagementHttpServices,
    create_device_management_router,
)
from hub.interfaces.http.routers.device_onboarding import (
    DeviceOnboardingHttpServices,
    create_device_onboarding_router,
)
from hub.ports.identity import ManagementPermission


@dataclass(frozen=True, slots=True)
class ComposedHttpRuntime:
    device_onboarding: DeviceOnboardingHttpServices
    management: DeviceManagementHttpServices
    device_erase: DeviceEraseHttpServices
    admission: AdmissionAuthority
    commissioning_ready: bool
    admission_actor: JwtAdmissionActorProvider


def create_composed_app(config: HubConfig | None = None) -> FastAPI:
    app_config = config or load_hub_config()
    runtime: ComposedHttpRuntime | None = None

    def require_runtime() -> ComposedHttpRuntime:
        if runtime is None:
            raise RuntimeError("Hub application runtime is not started")
        return runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal runtime
        secrets = load_runtime_secrets()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            resources = await open_runtime_resources(app_config, stack)
            projector = ProjectDeviceDirectory(
                devices=resources.repositories.devices,
                directory=resources.directory,
            )
            await projector.execute_all()
            device_onboarding = build_device_onboarding(
                config=app_config,
                clock=resources.clock,
            )
            management = build_device_management(
                repositories=resources.repositories,
                directory=resources.directory,
                projector=projector,
                management_jwt_secret=secrets.management_jwt,
                device_registry_reader_token=secrets.device_registry_reader_token,
                clock=resources.clock,
                ids=resources.ids,
            )
            admission = AdmissionAuthority(
                store=SqlAdmissionStore(resources.database),
                clock=resources.clock,
                ids=resources.ids,
                owner_domain_id=app_config.onboarding.owner_domain_id,
                owner_domain_generation=app_config.onboarding.owner_domain_generation,
                commissioning_proofs=resources.commissioning_proofs,
                claim_directory_projector=projector.execute,
            )
            channel_provider = ChannelProviderHttpClient(
                resources.http_client,
                token=os.environ.get("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", ""),
                contract_url=os.environ.get(
                    "EIDOLON_HUB_CHANNEL_PROVIDER_URL",
                    "http://127.0.0.1:8767/v1",
                ),
            )
            channel_revocations = PeriodicChannelRevocationReconcile(
                ReconcileChannelRevocations(
                    store=resources.repositories.channel_revocations,
                    provider=channel_provider,
                    clock=resources.clock,
                )
            )
            await channel_revocations.start()
            stack.push_async_callback(channel_revocations.stop)

            erase_reconcile = ReconcileDeviceEraseOperations(
                ledger=resources.repositories.device_erase,
                clock=resources.clock,
                operation_ttl=timedelta(
                    seconds=app_config.device_control.erase_operation_ttl_seconds
                ),
            )
            device_erase = DeviceEraseHttpServices(
                configuration=PullDeviceConfiguration(
                    claims=resources.repositories.device_erase,
                ),
                channel_binding=ReconcileChannelBinding(
                    devices=resources.repositories.devices,
                    provider=channel_provider,
                    clock=resources.clock,
                ),
                pull=PullDeviceEraseOperation(
                    ledger=resources.repositories.device_erase,
                    clock=resources.clock,
                ),
                acknowledge=AcknowledgeDeviceEraseOperation(
                    ledger=resources.repositories.device_erase,
                    clock=resources.clock,
                ),
                reconcile=erase_reconcile,
                ledger=resources.repositories.device_erase,
                authorizer=management.authorizer,
            )
            periodic_erase = PeriodicDeviceEraseReconcile(
                erase_reconcile,
                interval_seconds=app_config.device_control.erase_reconcile_poll_seconds,
            )
            await periodic_erase.start()
            stack.push_async_callback(periodic_erase.stop)

            if device_onboarding.mdns_advertiser is not None:
                await device_onboarding.mdns_advertiser.start()
                stack.push_async_callback(device_onboarding.mdns_advertiser.stop)

            runtime = ComposedHttpRuntime(
                device_onboarding=device_onboarding.http_services,
                management=management,
                device_erase=device_erase,
                admission=admission,
                commissioning_ready=resources.commissioning_ready,
                admission_actor=JwtAdmissionActorProvider(secret=secrets.management_jwt),
            )
            yield
        finally:
            runtime = None
            await stack.aclose()

    app = FastAPI(
        title="Eidolon Hub Device Management API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def safe_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default 422 body includes rejected proof and key inputs, so
        # no input value is safe to reflect or hand to an access-log collector.
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "url"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(errors)},
        )

    app.include_router(create_device_onboarding_router(lambda: require_runtime().device_onboarding))

    async def admission_actor(request: Request):
        return await require_runtime().admission_actor(request)

    async def claim_event_reader(request: Request):
        credential = request.headers.get("Authorization", "")
        try:
            await require_runtime().management.authorizer.authorize(
                credential=credential,
                permission=ManagementPermission.CLAIM_EVENTS,
                owner_scope=None,
                device_id=None,
            )
        except PermissionError as exc:
            raise AdmissionProblem(
                "UNAUTHENTICATED",
                "invalid Claim event reader credential",
                status=401,
                category="auth",
            ) from exc
        return require_runtime().admission.owner_domain_id

    app.include_router(
        create_admission_router(
            authority=lambda: require_runtime().admission,
            actor_provider=admission_actor,
            claim_event_reader_provider=claim_event_reader,
        )
    )
    app.include_router(
        create_device_management_router(services=lambda: require_runtime().management)
    )
    app.include_router(create_device_erase_router(services=lambda: require_runtime().device_erase))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, str]:
        if not require_runtime().commissioning_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "not-ready",
                "reason": "commissioning-proof-verifier-unavailable",
            }
        return {"status": "ready"}

    return app
