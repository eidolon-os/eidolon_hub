"""Owner Domain discovery assembly; mutations live in canonical Admission."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from hub.adapters.discovery.zeroconf import ZeroconfAuthorityCandidateAdvertiser
from hub.adapters.security.owner_directory import load_owner_directory
from hub.config import HubConfig
from hub.interfaces.http.routers.device_onboarding import DeviceOnboardingHttpServices
from hub.ports.identity import Clock

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
    clock: Clock,
) -> DeviceOnboardingGraph:
    owner_directory = load_owner_directory(config.onboarding, now=clock.now())
    return DeviceOnboardingGraph(
        http_services=DeviceOnboardingHttpServices(
            descriptor=owner_directory.descriptor,
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
