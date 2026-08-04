"""Outbound Channel Provider contract assembly."""

from __future__ import annotations

import httpx

from hub.adapters.channels.provider_client import (
    ChannelProviderHttpClient,
    HttpRequestReplyClient,
)
from hub.config import HubConfig


def build_channel_provider(
    *,
    config: HubConfig,
    http_client: httpx.AsyncClient,
    provider_token: str,
) -> ChannelProviderHttpClient:
    request_reply = HttpRequestReplyClient(http_client, bearer_token=provider_token)
    return ChannelProviderHttpClient(
        request_reply,
        contract_url=config.channel_provider.contract_url,
    )
