from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from hub.composition.app import create_composed_app
from hub.config import HubConfig, PersistenceConfig


def test_three_plane_openapi_routes_exist_before_lifespan_start() -> None:
    app = create_composed_app(HubConfig())

    paths = app.openapi()["paths"]

    assert "/api/connection/v1/descriptor" in paths
    assert "/api/connection/v1/register" in paths
    assert "/api/connection/v1/signals" not in paths
    assert "/api/connection/v1/signals/{device_id}" in paths
    assert "/api/device-management/v1/directory/{owner_scope}" in paths
    assert "/api/device-management/v1/events/{owner_scope}" in paths
    assert "/api/device-management/v1/devices/{device_id}/channels/{profile_name}" not in paths
    assert "/api/device-management/v1/devices/{device_id}/commands" in paths
    assert "/api/device-management/v1/commands/{command_id}" in paths
    assert "/api/device-management/v1/devices/{device_id}/approval" in paths
    assert "/api/device-management/v1/devices/{device_id}/revocation" in paths
    assert "/api/provider/v1/data/inbound" in paths
    assert "/api/provider/v1/channels/lifecycle" in paths


def test_production_app_does_not_start_telemetry_export_without_endpoint() -> None:
    config = HubConfig()
    app = create_composed_app(config)

    middleware_names = {item.cls.__name__ for item in app.user_middleware}

    assert "OpenTelemetryHttpMiddleware" in middleware_names
    assert config.observability.otlp_endpoint == ""


def test_composition_starts_with_only_hub_owned_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_HUB_LEASE_SECRET", "l" * 32)
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", "m" * 32)
    monkeypatch.setenv("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", "p" * 32)
    config = replace(
        HubConfig(),
        observability=replace(HubConfig().observability, enabled=False),
        connection_plane=replace(
            HubConfig().connection_plane,
            mdns=replace(HubConfig().connection_plane.mdns, enabled=False),
        ),
        persistence=PersistenceConfig(
            adapter="sqlite",
            sqlite_path=str(tmp_path / "runtime" / "hub.sqlite3"),
            reconciliation_seconds=60,
        ),
    )

    with TestClient(create_composed_app(config)) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert (tmp_path / "runtime" / "hub.sqlite3").is_file()
