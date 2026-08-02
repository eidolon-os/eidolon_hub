"""Production composition root for Hub device access and management."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from hub.adapters.devices.directory_worker import DeviceDirectoryProjectionWorker
from hub.adapters.observability.opentelemetry import (
    OpenTelemetryHttpMiddleware,
    OpenTelemetryRuntime,
    configure_opentelemetry,
)
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.composition.channel_control import build_channel_control
from hub.composition.device_access import build_device_access
from hub.composition.management import build_device_management
from hub.composition.resources import (
    load_runtime_environment,
    load_runtime_secrets,
    open_runtime_resources,
)
from hub.config import HubConfig, load_hub_config
from hub.interfaces.http.routers.device_access import (
    DeviceAccessHttpServices,
    create_device_access_router,
)
from hub.interfaces.http.routers.device_management import (
    DeviceManagementHttpServices,
    create_device_management_router,
)
from hub.interfaces.http.routers.provider_gateway import (
    ProviderGatewayHttpServices,
    create_provider_gateway_router,
)


@dataclass(frozen=True, slots=True)
class ComposedHttpRuntime:
    device_access: DeviceAccessHttpServices
    management: DeviceManagementHttpServices
    provider: ProviderGatewayHttpServices


def create_composed_app(config: HubConfig | None = None) -> FastAPI:
    app_config = config or load_hub_config()
    runtime: ComposedHttpRuntime | None = None
    telemetry: OpenTelemetryRuntime | None = None

    def require_runtime() -> ComposedHttpRuntime:
        if runtime is None:
            raise RuntimeError("Hub application runtime is not started")
        return runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal runtime, telemetry
        secrets = load_runtime_secrets()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            environment = load_runtime_environment(app_config)
            telemetry = configure_opentelemetry(
                enabled=app_config.observability.enabled,
                service_name=app_config.observability.service_name,
                endpoint=environment.otlp_endpoint,
            )
            stack.callback(telemetry.shutdown)
            resources = await open_runtime_resources(app_config, stack)
            projector = ProjectDeviceDirectory(
                devices=resources.repositories.devices,
                sessions=resources.repositories.sessions,
                directory=resources.directory,
                clock=resources.clock,
            )
            channel_control = build_channel_control(
                config=app_config,
                repositories=resources.repositories,
                http_client=resources.http_client,
                provider_token=secrets.provider_token,
                clock=resources.clock,
            )
            device_access = build_device_access(
                config=app_config,
                repositories=resources.repositories,
                projector=projector,
                provider=channel_control.provider,
                clock=resources.clock,
                ids=resources.ids,
                lease_secret=secrets.lease,
                hub_instance_id=environment.hub_instance_id,
            )
            management = build_device_management(
                repositories=resources.repositories,
                directory=resources.directory,
                projector=projector,
                bridge=channel_control.bridge,
                management_jwt_secret=secrets.management_jwt,
                clock=resources.clock,
                ids=resources.ids,
            )
            directory_worker = DeviceDirectoryProjectionWorker(
                projector,
                interval_seconds=app_config.device_directory.projection_interval_seconds,
            )

            if device_access.mdns_advertiser is not None:
                await device_access.mdns_advertiser.start()
                stack.push_async_callback(device_access.mdns_advertiser.stop)
            await directory_worker.start()
            stack.push_async_callback(directory_worker.stop)

            runtime = ComposedHttpRuntime(
                device_access=device_access.http_services,
                management=management,
                provider=channel_control.http_services,
            )
            yield
        finally:
            runtime = None
            telemetry = None
            await stack.aclose()

    app = FastAPI(
        title="Eidolon Hub Device Management API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(OpenTelemetryHttpMiddleware, runtime=lambda: telemetry)
    app.include_router(create_device_access_router(lambda: require_runtime().device_access))
    app.include_router(
        create_device_management_router(services=lambda: require_runtime().management)
    )
    app.include_router(create_provider_gateway_router(lambda: require_runtime().provider))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
