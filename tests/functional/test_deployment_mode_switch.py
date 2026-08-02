from __future__ import annotations

from hub.composition.app import create_composed_app
from hub.config import (
    DeploymentConfig,
    DeviceAccessConfig,
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    PostgresqlPersistenceConfig,
    SqlitePersistenceConfig,
)


def test_local_and_cloud_configs_publish_identical_device_bus_contracts() -> None:
    local = HubConfig(persistence=SqlitePersistenceConfig(path="var/local-test.sqlite3"))
    cloud = HubConfig(
        deployment=DeploymentConfig(mode="cloud"),
        persistence=PostgresqlPersistenceConfig(),
        discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        device_access=DeviceAccessConfig(public_base_url="https://hub.example.com"),
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

    sqlite_fields = set(SqlitePersistenceConfig.model_fields)
    postgresql_fields = set(PostgresqlPersistenceConfig.model_fields)

    assert "migration_source" not in sqlite_fields | postgresql_fields
    assert "bridge_to_cloud" not in sqlite_fields | postgresql_fields
    assert "replicate_sessions" not in sqlite_fields | postgresql_fields
