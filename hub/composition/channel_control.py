"""External Channel Provider contract assembly."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hub.adapters.channels.data_bridge import HttpDataEnvelopeSender, ProviderDataChannelBridge
from hub.adapters.channels.grant_sender import GrantSignalingRouter
from hub.adapters.channels.provider_client import (
    ChannelProviderHttpClient,
    HttpRequestReplyClient,
)
from hub.adapters.channels.reconcile_worker import DeviceChannelReconcileWorker
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.application.use_cases.ingest_data_envelope import IngestDataEnvelope
from hub.application.use_cases.reconcile_device_channels import ReconcileDeviceChannels
from hub.application.use_cases.record_channel_lifecycle import RecordChannelLifecycle
from hub.config import HubConfig
from hub.interfaces.http.routers.provider_gateway import ProviderGatewayHttpServices
from hub.ports.identity import Clock


@dataclass(frozen=True, slots=True)
class ChannelControlGraph:
    bridge: ProviderDataChannelBridge
    http_services: ProviderGatewayHttpServices
    worker: DeviceChannelReconcileWorker


def build_channel_control(
    *,
    config: HubConfig,
    repositories: SqlHubRepositories,
    http_client: httpx.AsyncClient,
    grant_sender: GrantSignalingRouter,
    provider_token: str,
    clock: Clock,
) -> ChannelControlGraph:
    request_reply = HttpRequestReplyClient(http_client, bearer_token=provider_token)
    provider = ChannelProviderHttpClient(
        request_reply,
        contract_url=config.channel_provider.contract_url,
    )
    reconciler = ReconcileDeviceChannels(
        hub_id=config.connection_plane.hub_id,
        hub_instance_id=config.connection_plane.hub_instance_id,
        provider=provider,
        grant_sender=grant_sender,
        devices=repositories.devices,
        connections=repositories.connections,
        syncs=repositories.channel_provider_sync,
        channel_leases=repositories.channel_leases,
        clock=clock,
    )
    ingest = IngestDataEnvelope(
        channels=repositories.channel_leases,
        commands=repositories.commands,
        events=repositories.events,
        clock=clock,
    )
    bridge = ProviderDataChannelBridge(
        sender=HttpDataEnvelopeSender(
            http_client,
            route=f"{config.channel_provider.contract_url}/data/envelopes",
            bearer_token=provider_token,
        ),
        channels=repositories.channel_leases,
        cursors=repositories.channel_cursors,
        ingest=ingest,
        clock=clock,
    )
    return ChannelControlGraph(
        bridge=bridge,
        http_services=ProviderGatewayHttpServices(
            bridge=bridge,
            lifecycle=RecordChannelLifecycle(leases=repositories.channel_leases),
            bearer_token=provider_token,
        ),
        worker=DeviceChannelReconcileWorker(reconciler),
    )
