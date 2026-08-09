from __future__ import annotations

from fastapi.testclient import TestClient

from hub.composition.app import create_composed_app
from hub.config import (
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    PersistenceConfig,
)


def test_public_contract_routes_exist_before_lifespan_start() -> None:
    app = create_composed_app(HubConfig())

    paths = app.openapi()["paths"]

    assert "/api/device-onboarding/v1/descriptor" in paths
    assert "/api/device-onboarding/v1/enrollments" in paths
    assert "/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff" in paths
    assert not any(path.startswith("/api/device-access/") for path in paths)
    assert not any("signals" in path for path in paths)
    assert "/api/device-management/v1/owners/{owner_scope}/devices" in paths
    assert "/api/device-management/v1/owners/{owner_scope}/devices/{device_id}" in paths
    assert "/api/device-management/v1/owners/{owner_scope}/events" in paths
    assert "/api/device-management/v1/directory/{owner_scope}" not in paths
    assert "/api/device-management/v1/events/{owner_scope}" not in paths
    assert "/api/device-management/v1/devices/{device_id}/channels/{profile_name}" not in paths
    assert "/api/device-management/v1/devices/{device_id}/commands" not in paths
    assert "/api/device-management/v1/commands/{command_id}" not in paths
    assert "/api/device-management/v1/devices/{device_id}/approval" in paths
    assert "/api/device-management/v1/devices/{device_id}/revocation" in paths
    assert not any("pairing-claims" in path for path in paths)
    assert "/api/provider/v1/data/inbound" not in paths
    assert not any(path.startswith("/api/provider/") for path in paths)


def test_composition_starts_with_only_hub_owned_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", "m" * 32)
    monkeypatch.setenv("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", "p" * 32)
    monkeypatch.setenv("EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN", "r" * 32)
    config = HubConfig(
        discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        persistence=PersistenceConfig(path=str(tmp_path / "runtime" / "hub.sqlite3")),
    )

    with TestClient(create_composed_app(config)) as client:
        assert client.get("/health").json() == {"status": "ok"}

        rejected_secret = "retrieval-secret-must-not-be-reflected"
        invalid = client.post(
            "/api/device-onboarding/v1/enrollments",
            json={
                "operation": "device.enrollment",
                "request_id": "invalid-secret-1",
                "retrieval_token": rejected_secret + "x" * 300,
                "identity": {"device_id": "device-1"},
                "manifest": {"schema_version": 1},
                "device_kind": "generic",
            },
        )
        assert invalid.status_code == 422
        assert rejected_secret not in invalid.text
        assert '"input"' not in invalid.text

    assert (tmp_path / "runtime" / "hub.sqlite3").is_file()
