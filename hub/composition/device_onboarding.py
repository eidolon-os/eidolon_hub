"""HTTPS device onboarding and local discovery assembly."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from hub.adapters.discovery.zeroconf import ZeroconfAuthorityCandidateAdvertiser
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.adapters.security.owner_directory import load_owner_directory
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.enroll_device import EnrollDevice
from hub.application.use_cases.handoff_device import HandoffDevice
from hub.application.use_cases.provision_device_channels import ProvisionDeviceChannels
from hub.config import HubConfig
from hub.interfaces.http.routers.device_onboarding import DeviceOnboardingHttpServices
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock, IdGenerator

_MDNS_SERVICE_TYPE = "_eidolon-owner._tcp.local."


def _mdns_target_hostname(owner_domain_id: str, public_hostname: str) -> str:
    """Return the local SRV target, independent of the TLS identity in TXT."""
    if public_hostname.endswith(".local"):
        return public_hostname.removesuffix(".local")
    label = re.sub(r"[^a-z0-9-]+", "-", owner_domain_id.lower()).strip("-")
    return (label or "eidolon-owner")[:63].rstrip("-")


@dataclass(frozen=True, slots=True)
class DeviceOnboardingGraph:
    http_services: DeviceOnboardingHttpServices
    mdns_advertiser: ZeroconfAuthorityCandidateAdvertiser | None


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
    owner_directory = load_owner_directory(config.onboarding, now=clock.now())
    provision = ProvisionDeviceChannels(
        owner_domain_id=owner_directory.owner_domain_id,
        provider=provider,
        clock=clock,
    )
    return DeviceOnboardingGraph(
        http_services=DeviceOnboardingHttpServices(
            descriptor=owner_directory.descriptor,
            enroll=EnrollDevice(
                devices=repositories.devices,
                mutations=repositories.device_mutations,
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
        mdns_advertiser=_mdns_advertiser(config),
    )


def _mdns_advertiser(
    config: HubConfig,
) -> ZeroconfAuthorityCandidateAdvertiser | None:
    if not config.discovery.mdns.enabled:
        return None
    public_url = urlparse(config.onboarding.descriptor_uri)
    public_hostname = public_url.hostname or ""
    return ZeroconfAuthorityCandidateAdvertiser(
        advertisement_id="mdns-local",
        service_type=_MDNS_SERVICE_TYPE,
        service_name=f"{config.onboarding.owner_domain_id}.{_MDNS_SERVICE_TYPE}",
        hostname=_mdns_target_hostname(
            config.onboarding.owner_domain_id, public_hostname
        ),
        port=public_url.port or 443,
        owner_domain_id=config.onboarding.owner_domain_id,
        owner_domain_descriptor_uri=config.onboarding.descriptor_uri,
    )
