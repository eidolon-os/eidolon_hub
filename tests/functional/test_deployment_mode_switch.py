from __future__ import annotations

from dataclasses import replace

from hub.composition.app import create_composed_app
from hub.config import HubConfig, PersistenceConfig


def test_local_and_cloud_configs_publish_identical_device_bus_contracts() -> None:
    local = HubConfig(
        persistence=PersistenceConfig(adapter="sqlite", sqlite_path="var/local-test.sqlite3")
    )
    cloud = replace(
        HubConfig(),
        persistence=PersistenceConfig(
            adapter="postgresql",
            postgresql_dsn_env="EIDOLON_HUB_TEST_POSTGRES_DSN",
        ),
        discovery=replace(
            HubConfig().discovery,
            mdns=replace(HubConfig().discovery.mdns, enabled=False),
        ),
    )

    local_openapi = create_composed_app(local).openapi()
    cloud_openapi = create_composed_app(cloud).openapi()

    assert local_openapi["paths"] == cloud_openapi["paths"]
    assert local_openapi["components"]["schemas"] == cloud_openapi["components"]["schemas"]
    assert local.persistence.adapter == "sqlite"
    assert cloud.persistence.adapter == "postgresql"
    assert local.discovery.mdns.enabled is True
    assert cloud.discovery.mdns.enabled is False


def test_mode_switch_has_no_data_or_live_lease_migration_semantics() -> None:
    """A config switch selects adapters; it is not an implicit data bridge."""

    persistence_fields = set(PersistenceConfig.__dataclass_fields__)

    assert "migration_source" not in persistence_fields
    assert "bridge_to_cloud" not in persistence_fields
    assert "replicate_sessions" not in persistence_fields
