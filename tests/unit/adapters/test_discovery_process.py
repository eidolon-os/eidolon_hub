import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub import discovery
from hub.adapters.persistence.database import HubDatabase
from hub.config import HubConfig
from hub.composition import device_onboarding


async def test_discovery_runs_and_shuts_down_without_initializing_application_database(monkeypatch):
    monkeypatch.setenv("EIDOLON_HUB_DISCOVERY_OWNER", "standalone")
    monkeypatch.setattr(discovery, "load_hub_config", lambda: HubConfig())
    database_start = AsyncMock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr(HubDatabase, "initialize_schema", database_start)
    started = asyncio.Event()
    publisher = AsyncMock()
    publisher.start.side_effect = started.set
    monkeypatch.setattr(discovery, "build_mdns_advertiser", lambda _: publisher)
    task = asyncio.create_task(discovery.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not task.done()
        database_start.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    publisher.stop.assert_awaited_once()


async def test_standalone_discovery_refuses_ambiguous_publisher_ownership(monkeypatch):
    monkeypatch.delenv("EIDOLON_HUB_DISCOVERY_OWNER", raising=False)
    with pytest.raises(RuntimeError, match="exclusive publisher ownership"):
        await discovery.run()


def test_api_does_not_start_a_second_publisher(monkeypatch):
    monkeypatch.setenv("EIDOLON_HUB_DISCOVERY_OWNER", "standalone")
    monkeypatch.setattr(device_onboarding, "load_owner_directory",
                        lambda *a, **k: SimpleNamespace(descriptor=None))
    monkeypatch.setattr(device_onboarding, "DeviceOnboardingHttpServices", lambda **k: k)
    def forbidden(_):
        raise AssertionError("API must not construct a second publisher")
    monkeypatch.setattr(device_onboarding, "build_mdns_advertiser", forbidden)
    graph = device_onboarding.build_device_onboarding(
        config=HubConfig(), clock=SimpleNamespace(now=lambda: None))
    assert graph.mdns_advertiser is None
