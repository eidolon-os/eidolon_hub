"""Production composition root for the three-plane Hub."""

from __future__ import annotations

import os
import ssl
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx
from fastapi import FastAPI

from hub.adapters.channels.data_bridge import HttpDataEnvelopeSender, ProviderDataChannelBridge
from hub.adapters.channels.grant_sender import (
    GrantSignalingRouter,
    HttpMailboxTransport,
    MqttSignalingTransport,
)
from hub.adapters.channels.provisioner_client import HttpRequestReplyClient, ProvisionerClient
from hub.adapters.connections.http import (
    HttpConnectionServices,
    HttpSignalMailbox,
    create_connection_router,
)
from hub.adapters.connections.mqtt import (
    Mqtt5Connector,
    create_mqtt_application_handler,
)
from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.adapters.observability.opentelemetry import (
    OpenTelemetryHttpMiddleware,
    OpenTelemetryRuntime,
    configure_opentelemetry,
)
from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import CachedDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.connection_proof import (
    HmacLeaseCredentialIssuer,
    P256ConnectionProofVerifier,
)
from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.close_connection import CloseConnection
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.get_command import GetCommand
from hub.application.use_cases.handle_channel_signal import HandleChannelSignal
from hub.application.use_cases.ingest_data_envelope import IngestDataEnvelope
from hub.application.use_cases.provision_channel import ProvisionChannel
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.application.use_cases.revoke_channel import RevokeChannel
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.application.use_cases.revoke_device_channels import RevokeDeviceChannels
from hub.application.use_cases.send_command import SendCommand
from hub.composition.runtime import SecureIdGenerator, SystemClock
from hub.config import HubConfig, load_hub_config, validate_hub_config
from hub.contracts.bindings.connection import HubDescriptor
from hub.domain.channels.entities import ChannelKind, ChannelProfile
from hub.domain.channels.selection import ChannelProfileCatalog
from hub.interfaces.http.routers.device_management import (
    DeviceManagementHttpServices,
    create_device_management_router,
)
from hub.interfaces.http.routers.provider_gateway import (
    ProviderGatewayHttpServices,
    create_provider_gateway_router,
)
from hub.ports.connections import ConnectorSupervisor


def _lease_secret() -> bytes:
    value = os.environ.get("EIDOLON_HUB_LEASE_SECRET", "").encode()
    if len(value) < 32:
        raise RuntimeError("EIDOLON_HUB_LEASE_SECRET must contain at least 32 bytes")
    return value


def _management_jwt_secret() -> bytes:
    value = os.environ.get("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", "").encode()
    if len(value) < 32:
        raise RuntimeError("EIDOLON_HUB_MANAGEMENT_JWT_SECRET must contain at least 32 bytes")
    return value


def _provider_token(config: HubConfig) -> str:
    value = os.environ.get(config.channel_control.provider_token_env, "")
    if len(value.encode()) < 32:
        raise RuntimeError(
            f"{config.channel_control.provider_token_env} must contain at least 32 bytes"
        )
    return value


def _database(config: HubConfig) -> HubDatabase:
    persistence = config.persistence
    if persistence.adapter == "sqlite":
        return HubDatabase.sqlite(persistence.sqlite_path)
    dsn = os.environ.get(persistence.postgresql_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(f"{persistence.postgresql_dsn_env} is required")
    return HubDatabase.postgresql(
        dsn,
        pool_size=persistence.pool_size,
        max_overflow=persistence.max_overflow,
    )


def _profiles(config: HubConfig) -> tuple[ChannelProfile, ...]:
    return tuple(
        ChannelProfile(
            name=name,
            required_kinds=frozenset(ChannelKind(value) for value in item.required_kinds),
            provisioner_ref=item.provisioner_ref,
        )
        for name, item in config.channel_control.profiles.items()
    )


@dataclass(frozen=True, slots=True)
class ApplicationHttpRuntime:
    connections: HttpConnectionServices
    management: DeviceManagementHttpServices
    provider: ProviderGatewayHttpServices


def create_composed_app(config: HubConfig | None = None) -> FastAPI:
    app_config = config or load_hub_config()
    validate_hub_config(app_config)
    runtime: ApplicationHttpRuntime | None = None
    telemetry: OpenTelemetryRuntime | None = None

    def require_runtime() -> ApplicationHttpRuntime:
        if runtime is None:
            raise RuntimeError("Hub application runtime is not started")
        return runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal runtime, telemetry
        lease_secret = _lease_secret()
        management_jwt_secret = _management_jwt_secret()
        provider_token = _provider_token(app_config)
        configured_profiles = _profiles(app_config)
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            telemetry = configure_opentelemetry(
                enabled=app_config.observability.enabled,
                service_name=app_config.observability.service_name,
                endpoint=app_config.observability.otlp_endpoint,
            )
            stack.callback(telemetry.shutdown)
            database = _database(app_config)
            stack.push_async_callback(database.close)
            if app_config.persistence.init_schema:
                await database.init_schema()
            http_client = await stack.enter_async_context(httpx.AsyncClient())
        except BaseException:
            await stack.aclose()
            raise
        clock = SystemClock()
        ids = SecureIdGenerator()
        repositories = SqlHubRepositories(database)
        connections = repositories.connections
        authority = repositories.authority
        challenges = repositories.challenges
        channel_leases = repositories.channel_leases
        channel_cursors = repositories.channel_cursors
        events = repositories.events
        directory = repositories.directory
        if app_config.persistence.directory_cache_enabled:
            cached_directory = CachedDeviceDirectoryRepository(
                repositories.directory,
                reconciliation_seconds=app_config.persistence.reconciliation_seconds,
            )
            await cached_directory.start()
            stack.push_async_callback(cached_directory.stop)
            directory = cached_directory
        profile_catalog = ChannelProfileCatalog(configured_profiles)
        request_reply = HttpRequestReplyClient(http_client, bearer_token=provider_token)
        provisioners = {
            reference: ProvisionerClient(
                request_reply,
                route=f"{endpoint}/v1/channels/operations",
            )
            for reference, endpoint in app_config.channel_control.provider_endpoints.items()
        }
        revoke_channel = RevokeChannel(
            profiles=profile_catalog,
            provisioners=provisioners,
            leases=channel_leases,
        )
        revoke_device_channels = RevokeDeviceChannels(
            leases=channel_leases,
            revoke_channel=revoke_channel,
        )
        projector = ProjectDeviceDirectory(
            devices=repositories.devices,
            connections=connections,
            directory=directory,
            clock=clock,
        )
        enroll = EnrollDevice(
            challenges=challenges,
            connections=connections,
            authority=authority,
            proof_verifier=P256ConnectionProofVerifier(),
            credential_issuer=HmacLeaseCredentialIssuer(lease_secret),
            clock=clock,
            ids=ids,
            hub_instance_id=app_config.connection_plane.hub_instance_id,
            connection_ttl=timedelta(seconds=app_config.connection_plane.lease_seconds),
        )
        register = RegisterDevice(
            devices=repositories.devices,
            connections=connections,
            events=events,
            clock=clock,
            directory_projector=projector,
        )
        renew = RenewConnection(
            connections=connections,
            authority=authority,
            events=events,
            clock=clock,
            ttl=timedelta(seconds=app_config.connection_plane.lease_seconds),
            directory_projector=projector,
        )
        close = CloseConnection(
            connections=connections,
            events=events,
            clock=clock,
            channel_revoker=revoke_device_channels,
            directory_projector=projector,
        )
        authenticate_connection = AuthenticateConnection(
            connections=connections,
            clock=clock,
        )
        handle_channel_signal = HandleChannelSignal(
            channels=channel_leases,
            revoke_channel=revoke_channel,
            events=events,
            clock=clock,
        )
        base_url = app_config.connection_plane.public_base_url.rstrip("/")
        descriptor = HubDescriptor(
            hub_id=app_config.connection_plane.hub_id,
            descriptor_uri=f"{base_url}/api/connection/v1/descriptor",
            https_registration_uri=f"{base_url}/api/connection/v1/register",
            mqtt_endpoint_uri=(
                f"mqtts://{app_config.connection_plane.mqtt.hostname}:"
                f"{app_config.connection_plane.mqtt.port}"
                if app_config.connection_plane.mqtt.enabled
                else None
            ),
        )
        mailbox = HttpSignalMailbox()
        mqtt_connector = None
        connectors = []
        if app_config.mdns.enabled:
            connectors.append(
                ZeroconfHubAdvertiser(
                    connector_id="mdns-local",
                    service_type=app_config.mdns.service_type,
                    service_name=(
                        app_config.mdns.service_name
                        or f"Eidolon Hub.{app_config.mdns.service_type}"
                    ),
                    hostname=app_config.mdns.hostname,
                    port=app_config.api.port,
                    descriptor_uri=descriptor.descriptor_uri,
                    registration_uri=descriptor.https_registration_uri,
                )
            )
        mqtt_config = app_config.connection_plane.mqtt
        if mqtt_config.enabled:
            mqtt_connector = Mqtt5Connector(
                connector_id=mqtt_config.connector_id,
                hostname=mqtt_config.hostname,
                port=mqtt_config.port,
                username=mqtt_config.username or None,
                password=os.environ.get(mqtt_config.password_env) or None,
                tls_context=ssl.create_default_context(),
                client_id=(
                    f"{app_config.connection_plane.hub_instance_id}-{mqtt_config.connector_id}"
                ),
                handler=create_mqtt_application_handler(
                    enroll=enroll,
                    register=register,
                    renew=renew,
                    close=close,
                    authenticate_connection=authenticate_connection,
                    handle_channel_signal=handle_channel_signal,
                    expected_connector_id=mqtt_config.connector_id,
                    heartbeat_after_ms=app_config.connection_plane.heartbeat_after_ms,
                    connector_priority=mqtt_config.priority,
                ),
            )
            connectors.append(mqtt_connector)
        supervisor = ConnectorSupervisor(tuple(connectors))
        transports = {"http-mailbox": HttpMailboxTransport(mailbox)}
        if mqtt_connector is not None:
            transports["mqtt"] = MqttSignalingTransport(mqtt_connector)
        provision_channel = ProvisionChannel(
            profiles=profile_catalog,
            provisioners=provisioners,
            grant_sender=GrantSignalingRouter(transports),
            devices=repositories.devices,
            connections=connections,
            channel_leases=channel_leases,
            clock=clock,
            ids=ids,
        )
        ingest_data = IngestDataEnvelope(
            channels=channel_leases,
            commands=repositories.commands,
            events=events,
            clock=clock,
        )
        management_profile = app_config.channel_control.profiles[
            app_config.channel_control.management_profile
        ]
        management_provider_endpoint = app_config.channel_control.provider_endpoints[
            management_profile.provisioner_ref
        ]
        data_bridge = ProviderDataChannelBridge(
            sender=HttpDataEnvelopeSender(
                http_client,
                route=f"{management_provider_endpoint}/v1/data/envelopes",
                bearer_token=provider_token,
            ),
            channels=channel_leases,
            cursors=channel_cursors,
            ingest=ingest_data,
            clock=clock,
            management_profile=app_config.channel_control.management_profile,
        )
        send_command = SendCommand(
            devices=repositories.devices,
            commands=repositories.commands,
            sender=data_bridge,
            clock=clock,
            ids=ids,
        )
        approve_device = ApproveDevice(
            devices=repositories.devices,
            events=events,
            clock=clock,
            directory_projector=projector,
        )
        revoke_device = RevokeDevice(
            devices=repositories.devices,
            connections=connections,
            channel_revoker=revoke_device_channels,
            events=events,
            clock=clock,
            directory_projector=projector,
        )
        try:
            await supervisor.start()
        except BaseException:
            await stack.aclose()
            raise
        stack.push_async_callback(supervisor.stop)
        runtime = ApplicationHttpRuntime(
            connections=HttpConnectionServices(
                descriptor=descriptor,
                enroll=enroll,
                register=register,
                renew=renew,
                mailbox=mailbox,
                authenticate_connection=authenticate_connection,
                handle_channel_signal=handle_channel_signal,
                heartbeat_after_ms=app_config.connection_plane.heartbeat_after_ms,
                connector_id="https-local",
            ),
            management=DeviceManagementHttpServices(
                directory=directory,
                provision_channel=provision_channel,
                send_command=send_command,
                get_command=GetCommand(repositories.commands),
                approve_device=approve_device,
                revoke_device=revoke_device,
                authorizer=JwtOwnerManagementAuthorizer(
                    secret=management_jwt_secret,
                    devices=repositories.devices,
                ),
                event_stream=events,
            ),
            provider=ProviderGatewayHttpServices(
                bridge=data_bridge,
                bearer_token=provider_token,
            ),
        )
        try:
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
    app.include_router(create_connection_router(lambda: require_runtime().connections))
    app.include_router(
        create_device_management_router(services=lambda: require_runtime().management)
    )
    app.include_router(create_provider_gateway_router(lambda: require_runtime().provider))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
