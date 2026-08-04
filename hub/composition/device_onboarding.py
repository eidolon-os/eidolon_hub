"""HTTPS device onboarding and local discovery assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from hub.adapters.discovery.zeroconf import ZeroconfHubAdvertiser
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.handoff_device import HandoffDevice
from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.config import HubConfig
from hub.contracts.bindings.onboarding import HubDescriptor
from hub.interfaces.http.routers.device_onboarding import DeviceOnboardingHttpServices
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock, IdGenerator

_MDNS_SERVICE_TYPE = "_eidolon-hub._tcp.local."


@dataclass(frozen=True, slots=True)
class DeviceOnboardingGraph:
    http_services: DeviceOnboardingHttpServices
    mdns_advertiser: ZeroconfHubAdvertiser | None


def build_device_onboarding(
    *,
    config: HubConfig,
    repositories: SqlHubRepositories,
    projector: ProjectDeviceDirectory,
    provider: ChannelProviderControl,
    clock: Clock,
    ids: IdGenerator,
) -> DeviceOnboardingGraph:
    tokens = Sha256RetrievalTokenHasher()
    retrieval_window = timedelta(seconds=config.onboarding.retrieval_window_seconds)
    provision = ProvisionDeviceChannels(
        hub_id=config.onboarding.hub_id,
        provider=provider,
        clock=clock,
    )
    descriptor = _hub_descriptor(config)
    return DeviceOnboardingGraph(
        http_services=DeviceOnboardingHttpServices(
            descriptor=descriptor,
            enroll=EnrollDevice(
                devices=repositories.devices,
                events=repositories.management_events,
                clock=clock,
                ids=ids,
                tokens=tokens,
                enrollment_ttl=retrieval_window,
                directory_projector=projector,
            ),
            handoff=HandoffDevice(
                devices=repositories.devices,
                provision=provision,
                tokens=tokens,
                clock=clock,
            ),
        ),
        mdns_advertiser=_mdns_advertiser(config, descriptor),
    )


def _hub_descriptor(config: HubConfig) -> HubDescriptor:
    base_url = config.onboarding.public_base_url.rstrip("/")
    onboarding_uri = f"{base_url}/api/device-onboarding/v1"
    return HubDescriptor(
        hub_id=config.onboarding.hub_id,
        descriptor_uri=f"{onboarding_uri}/descriptor",
        device_onboarding_uri=onboarding_uri,
        enrollment_uri=f"{onboarding_uri}/enrollments",
    )


def _mdns_advertiser(
    config: HubConfig,
    descriptor: HubDescriptor,
) -> ZeroconfHubAdvertiser | None:
    if not config.discovery.mdns.enabled:
        return None
    public_url = urlparse(config.onboarding.public_base_url)
    public_hostname = public_url.hostname or ""
    return ZeroconfHubAdvertiser(
        advertisement_id="mdns-local",
        service_type=_MDNS_SERVICE_TYPE,
        service_name=f"{config.onboarding.hub_id}.{_MDNS_SERVICE_TYPE}",
        hostname=public_hostname.removesuffix(".local"),
        port=public_url.port or 443,
        descriptor_uri=descriptor.descriptor_uri,
        enrollment_uri=descriptor.enrollment_uri,
    )
