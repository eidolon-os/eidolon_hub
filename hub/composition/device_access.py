"""HTTPS device-access and local discovery assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.session_proof import (
    HmacLeaseCredentialIssuer,
    P256SessionProofVerifier,
)
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.acquire_device_channels import AcquireDeviceChannels
from hub.application.use_cases.close_session import CloseDeviceSession
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_session import RenewDeviceSession
from hub.config import HubConfig
from hub.contracts.bindings.session import HubDescriptor
from hub.interfaces.http.routers.device_access import DeviceAccessHttpServices
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock, IdGenerator

_MDNS_SERVICE_TYPE = "_eidolon-hub._tcp.local."


@dataclass(frozen=True, slots=True)
class DeviceAccessGraph:
    http_services: DeviceAccessHttpServices
    mdns_advertiser: ZeroconfHubAdvertiser | None


def build_device_access(
    *,
    config: HubConfig,
    repositories: SqlHubRepositories,
    projector: ProjectDeviceDirectory,
    provider: ChannelProviderControl,
    clock: Clock,
    ids: IdGenerator,
    lease_secret: bytes,
    hub_instance_id: str,
) -> DeviceAccessGraph:
    sessions = repositories.sessions
    enroll = EnrollDevice(
        challenges=repositories.challenges,
        sessions=sessions,
        authority=repositories.authority,
        proof_verifier=P256SessionProofVerifier(),
        credential_issuer=HmacLeaseCredentialIssuer(lease_secret),
        clock=clock,
        ids=ids,
        hub_instance_id=hub_instance_id,
        session_ttl=timedelta(seconds=config.device_access.session_lease_seconds),
    )
    register = RegisterDevice(
        devices=repositories.devices,
        sessions=sessions,
        events=repositories.events,
        clock=clock,
        directory_projector=projector,
    )
    renew = RenewDeviceSession(
        sessions=sessions,
        authority=repositories.authority,
        events=repositories.events,
        clock=clock,
        ttl=timedelta(seconds=config.device_access.session_lease_seconds),
        directory_projector=projector,
    )
    close = CloseDeviceSession(
        sessions=sessions,
        events=repositories.events,
        clock=clock,
        directory_projector=projector,
    )
    acquire = AcquireDeviceChannels(
        hub_id=config.device_access.hub_id,
        devices=repositories.devices,
        sessions=sessions,
        channels=repositories.channel_leases,
        provider=provider,
        clock=clock,
    )
    descriptor = _hub_descriptor(config)
    return DeviceAccessGraph(
        http_services=DeviceAccessHttpServices(
            descriptor=descriptor,
            enroll=enroll,
            register=register,
            renew=renew,
            close=close,
            acquire_channels=acquire,
            heartbeat_after_ms=config.device_access.heartbeat_after_ms,
        ),
        mdns_advertiser=_mdns_advertiser(config, descriptor),
    )


def _hub_descriptor(config: HubConfig) -> HubDescriptor:
    base_url = config.device_access.public_base_url.rstrip("/")
    access_uri = f"{base_url}/api/device-access/v1"
    return HubDescriptor(
        hub_id=config.device_access.hub_id,
        descriptor_uri=f"{access_uri}/descriptor",
        device_access_uri=access_uri,
        registration_uri=f"{access_uri}/register",
        channel_acquisition_uri=f"{access_uri}/channels/acquire",
    )


def _mdns_advertiser(
    config: HubConfig,
    descriptor: HubDescriptor,
) -> ZeroconfHubAdvertiser | None:
    mdns = config.discovery.mdns
    if not mdns.enabled:
        return None
    public_url = urlparse(config.device_access.public_base_url)
    public_hostname = public_url.hostname or ""
    mdns_hostname = public_hostname.removesuffix(".local")
    public_port = public_url.port or 443
    return ZeroconfHubAdvertiser(
        advertisement_id="mdns-local",
        service_type=_MDNS_SERVICE_TYPE,
        service_name=f"{config.device_access.hub_id}.{_MDNS_SERVICE_TYPE}",
        hostname=mdns_hostname,
        port=public_port,
        descriptor_uri=descriptor.descriptor_uri,
        registration_uri=descriptor.registration_uri,
    )
