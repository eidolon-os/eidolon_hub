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
    assert "/api/device-onboarding/v1/enrollments" not in paths
    assert "/api/device-onboarding/v1/enrollments/{enrollment_id}/handoff" not in paths
    assert "/api/admission/v1/enrollments" in paths
    assert "/api/admission/v1/enrollments/{enrollment_id}/claim-grants:collect" in paths
    assert "/api/admission/v1/enrollments/{enrollment_id}/claim-grants/{grant_id}:ack" in paths
    assert "/api/admission/v1/claims/{device_instance_id}:revoke" in paths
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
    assert "/api/device-management/v1/devices/{device_id}/approval" not in paths
    assert "/api/device-management/v1/devices/{device_id}/revocation" not in paths
    assert "/api/device-management/v1/claim-events" not in paths
    assert not any("pairing-claims" in path for path in paths)
    assert "/api/provider/v1/data/inbound" not in paths
    assert not any(path.startswith("/api/provider/") for path in paths)


def test_composition_starts_with_only_hub_owned_sqlite(
    tmp_path, monkeypatch, owner_directory_config
) -> None:
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", "m" * 32)
    monkeypatch.setenv("EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", "p" * 32)
    monkeypatch.setenv("EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN", "r" * 32)
    config = HubConfig(
        onboarding=owner_directory_config(),
        discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        persistence=PersistenceConfig(path=str(tmp_path / "runtime" / "hub.sqlite3")),
    )

    with TestClient(create_composed_app(config)) as client:
        assert client.get("/health").json() == {"status": "ok"}

        unauthorized_events = client.get("/api/admission/v1/claim-events")
        assert unauthorized_events.status_code == 401
        authorized_events = client.get(
            "/api/admission/v1/claim-events",
            headers={"Authorization": f"Bearer {'r' * 32}"},
        )
        assert authorized_events.status_code == 200
        assert authorized_events.json()["events"] == []

        assert client.get("/ready").status_code == 503
        rejected_secret = "commissioning-proof-must-not-be-reflected"
        invalid = client.post(
            "/api/admission/v1/enrollments",
            json={
                "command_id": "invalid-secret-1",
                "correlation_id": "intent-1",
                "commissioning_proof": rejected_secret,
            },
        )
        assert invalid.status_code == 422
        assert rejected_secret not in invalid.text
        assert '"input"' not in invalid.text

    assert (tmp_path / "runtime" / "hub.sqlite3").is_file()
