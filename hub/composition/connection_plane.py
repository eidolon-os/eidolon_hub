"""Connection-plane service graph assembly."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from datetime import timedelta

from hub.adapters.channels.grant_sender import (
    GrantSignalingRouter,
    HttpMailboxTransport,
    MqttSignalingTransport,
)
from hub.adapters.connections.http import HttpConnectionServices, HttpSignalMailbox
from hub.adapters.connections.mqtt import Mqtt5Connector, create_mqtt_application_handler
from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.connection_proof import (
    HmacLeaseCredentialIssuer,
    P256ConnectionProofVerifier,
)
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.close_connection import CloseConnection
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.config import HubConfig
from hub.contracts.bindings.connection import HubDescriptor
from hub.ports.connections import ConnectorSupervisor
from hub.ports.identity import Clock, IdGenerator


@dataclass(frozen=True, slots=True)
class ConnectionPlaneGraph:
    http_services: HttpConnectionServices
    supervisor: ConnectorSupervisor
    grant_sender: GrantSignalingRouter


def build_connection_plane(
    *,
    config: HubConfig,
    repositories: SqlHubRepositories,
    projector: ProjectDeviceDirectory,
    clock: Clock,
    ids: IdGenerator,
    lease_secret: bytes,
) -> ConnectionPlaneGraph:
    connections = repositories.connections
    enroll = EnrollDevice(
        challenges=repositories.challenges,
        connections=connections,
        authority=repositories.authority,
        proof_verifier=P256ConnectionProofVerifier(),
        credential_issuer=HmacLeaseCredentialIssuer(lease_secret),
        clock=clock,
        ids=ids,
        hub_instance_id=config.connection_plane.hub_instance_id,
        connection_ttl=timedelta(seconds=config.connection_plane.lease_seconds),
    )
    register = RegisterDevice(
        devices=repositories.devices,
        connections=connections,
        events=repositories.events,
        clock=clock,
        directory_projector=projector,
    )
    renew = RenewConnection(
        connections=connections,
        authority=repositories.authority,
        events=repositories.events,
        clock=clock,
        ttl=timedelta(seconds=config.connection_plane.lease_seconds),
        directory_projector=projector,
    )
    close = CloseConnection(
        connections=connections,
        events=repositories.events,
        clock=clock,
        directory_projector=projector,
    )
    descriptor = _hub_descriptor(config)
    mailbox = HttpSignalMailbox()
    mqtt_connector = _mqtt_connector(
        config=config,
        enroll=enroll,
        register=register,
        renew=renew,
        close=close,
    )
    connectors = _discovery_connectors(config, descriptor)
    if mqtt_connector is not None:
        connectors.append(mqtt_connector)

    transports = {"http-mailbox": HttpMailboxTransport(mailbox)}
    if mqtt_connector is not None:
        transports["mqtt"] = MqttSignalingTransport(mqtt_connector)

    return ConnectionPlaneGraph(
        http_services=HttpConnectionServices(
            descriptor=descriptor,
            enroll=enroll,
            register=register,
            renew=renew,
            mailbox=mailbox,
            authenticate_connection=AuthenticateConnection(
                connections=connections,
                clock=clock,
            ),
            heartbeat_after_ms=config.connection_plane.heartbeat_after_ms,
            connector_id="https-local",
        ),
        supervisor=ConnectorSupervisor(tuple(connectors)),
        grant_sender=GrantSignalingRouter(transports),
    )


def _hub_descriptor(config: HubConfig) -> HubDescriptor:
    base_url = config.connection_plane.public_base_url.rstrip("/")
    mqtt = config.connection_plane.mqtt
    return HubDescriptor(
        hub_id=config.connection_plane.hub_id,
        descriptor_uri=f"{base_url}/api/connection/v1/descriptor",
        https_registration_uri=f"{base_url}/api/connection/v1/register",
        mqtt_endpoint_uri=f"mqtts://{mqtt.hostname}:{mqtt.port}" if mqtt.enabled else None,
    )


def _discovery_connectors(
    config: HubConfig,
    descriptor: HubDescriptor,
) -> list[ZeroconfHubAdvertiser]:
    mdns = config.connection_plane.mdns
    if not mdns.enabled:
        return []
    return [
        ZeroconfHubAdvertiser(
            connector_id="mdns-local",
            service_type=mdns.service_type,
            service_name=mdns.service_name or f"Eidolon Hub.{mdns.service_type}",
            hostname=mdns.hostname,
            port=config.api.port,
            descriptor_uri=descriptor.descriptor_uri,
            registration_uri=descriptor.https_registration_uri,
        )
    ]


def _mqtt_connector(
    *,
    config: HubConfig,
    enroll: EnrollDevice,
    register: RegisterDevice,
    renew: RenewConnection,
    close: CloseConnection,
) -> Mqtt5Connector | None:
    mqtt = config.connection_plane.mqtt
    if not mqtt.enabled:
        return None
    return Mqtt5Connector(
        connector_id=mqtt.connector_id,
        hostname=mqtt.hostname,
        port=mqtt.port,
        username=mqtt.username or None,
        password=os.environ.get(mqtt.password_env) or None,
        tls_context=ssl.create_default_context(),
        client_id=f"{config.connection_plane.hub_instance_id}-{mqtt.connector_id}",
        handler=create_mqtt_application_handler(
            enroll=enroll,
            register=register,
            renew=renew,
            close=close,
            expected_connector_id=mqtt.connector_id,
            heartbeat_after_ms=config.connection_plane.heartbeat_after_ms,
            connector_priority=mqtt.priority,
        ),
    )
