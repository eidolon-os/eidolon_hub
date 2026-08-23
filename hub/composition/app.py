"""Production composition root for Hub device onboarding and management."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.application.device_control_delivery import (
    DeliverDeviceControlOperations,
    PeriodicDeviceControlDelivery,
)
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.composition.channel_control import build_channel_provider
from hub.composition.device_onboarding import build_device_onboarding
from hub.composition.management import build_device_management
from hub.composition.resources import (
    load_runtime_secrets,
    open_runtime_resources,
)
from hub.config import HubConfig, load_hub_config
from hub.device_control.application import (
    AcknowledgeDeviceEraseOperation,
    BindDeviceOperationKey,
    PeriodicDeviceEraseReconcile,
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


@dataclass(frozen=True, slots=True)
class ComposedHttpRuntime:
    device_onboarding: DeviceOnboardingHttpServices
    management: DeviceManagementHttpServices
    device_erase: DeviceEraseHttpServices


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
            channel_provider = build_channel_provider(
                config=app_config,
                http_client=resources.http_client,
                provider_token=secrets.provider_token,
            )
            device_onboarding = build_device_onboarding(
                config=app_config,
                repositories=resources.repositories,
                projector=projector,
                provider=channel_provider,
                clock=resources.clock,
                ids=resources.ids,
            )
            management = build_device_management(
                repositories=resources.repositories,
                directory=resources.directory,
                projector=projector,
                management_jwt_secret=secrets.management_jwt,
                device_registry_reader_token=secrets.device_registry_reader_token,
                clock=resources.clock,
                ids=resources.ids,
                handoff_ttl=timedelta(
                    seconds=app_config.onboarding.retrieval_window_seconds
                ),
            )
            device_control_delivery = PeriodicDeviceControlDelivery(
                DeliverDeviceControlOperations(
                    store=resources.repositories.device_control,
                    provider=channel_provider,
                    clock=resources.clock,
                    retry_base_seconds=(
                        app_config.channel_provider.revoke_retry_base_seconds
                    ),
                    retry_max_seconds=(
                        app_config.channel_provider.revoke_retry_max_seconds
                    ),
                ),
                interval_seconds=(
                    app_config.channel_provider.revoke_delivery_poll_seconds
                ),
            )
            await device_control_delivery.start()
            stack.push_async_callback(device_control_delivery.stop)

            erase_reconcile = ReconcileDeviceEraseOperations(
                ledger=resources.repositories.device_erase,
                clock=resources.clock,
                operation_ttl=timedelta(
                    seconds=app_config.device_control.erase_operation_ttl_seconds
                ),
            )
            device_erase = DeviceEraseHttpServices(
                bind_key=BindDeviceOperationKey(
                    devices=resources.repositories.devices,
                    tokens=Sha256RetrievalTokenHasher(),
                    ledger=resources.repositories.device_erase,
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
        # FastAPI's default 422 body includes the rejected raw input.  Device
        # onboarding requests contain a retrieval secret, so no input value is
        # safe to reflect or hand to an access-log collector.
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "url"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(errors)},
        )

    app.include_router(
        create_device_onboarding_router(lambda: require_runtime().device_onboarding)
    )
    app.include_router(
        create_device_management_router(services=lambda: require_runtime().management)
    )
    app.include_router(
        create_device_erase_router(services=lambda: require_runtime().device_erase)
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
