"""Production composition root for Hub device onboarding and management."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.composition.channel_control import build_channel_provider
from hub.composition.device_onboarding import build_device_onboarding
from hub.composition.management import build_device_management
from hub.composition.resources import (
    load_runtime_secrets,
    open_runtime_resources,
)
from hub.config import HubConfig, load_hub_config
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
                provider=channel_provider,
                hub_id=app_config.onboarding.hub_id,
                management_jwt_secret=secrets.management_jwt,
                device_registry_reader_token=secrets.device_registry_reader_token,
                clock=resources.clock,
                handoff_ttl=timedelta(
                    seconds=app_config.onboarding.retrieval_window_seconds
                ),
            )

            if device_onboarding.mdns_advertiser is not None:
                await device_onboarding.mdns_advertiser.start()
                stack.push_async_callback(device_onboarding.mdns_advertiser.stop)

            runtime = ComposedHttpRuntime(
                device_onboarding=device_onboarding.http_services,
                management=management,
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
