from __future__ import annotations

from dataclasses import replace

import pytest

from hub.config import (
    ConnectionPlaneConfig,
    HubConfig,
    MqttConnectorConfig,
    PersistenceConfig,
    validate_hub_config,
)


def test_default_production_config_has_only_logical_channel_profiles() -> None:
    config = HubConfig()

    validate_hub_config(config)

    assert config.channel_control.management_profile == "management-data"
    assert config.channel_control.profiles["realtime-media"].required_kinds == (
        "realtime-data",
        "audio",
        "video",
    )


def test_enabled_mqtt_requires_a_broker_hostname_before_resources_start() -> None:
    config = HubConfig(
        connection_plane=ConnectionPlaneConfig(mqtt=MqttConnectorConfig(enabled=True, hostname=""))
    )

    with pytest.raises(ValueError, match="requires hostname"):
        validate_hub_config(config)


def test_channel_profile_must_reference_a_configured_provisioner() -> None:
    config = HubConfig(channel_control=replace(HubConfig().channel_control, provider_endpoints={}))

    with pytest.raises(ValueError, match="unknown provisioner"):
        validate_hub_config(config)


def test_database_adapter_is_explicitly_bounded() -> None:
    config = HubConfig(persistence=PersistenceConfig(adapter="redis"))

    with pytest.raises(ValueError, match="sqlite or postgresql"):
        validate_hub_config(config)


def test_production_yaml_loader_uses_new_directory_and_mdns_boundaries(
    tmp_path, monkeypatch
) -> None:
    settings = tmp_path / "settings.yaml"
    environment = tmp_path / ".env"
    settings.write_text(
        """
api: {port: 8443}
mdns: {enabled: false}
persistence:
  adapter: sqlite
  sqlite_path: runtime/test-hub.sqlite3
  directory_cache_enabled: false
connection_plane:
  public_base_url: https://hub.example
channel_control:
  management_profile: management-data
  provider_token_env: TEST_PROVIDER_TOKEN
  profiles:
    management-data:
      required_kinds: [reliable-data]
      provisioner_ref: provider/default
  provider_endpoints:
    provider/default: https://provider.example
""".strip(),
        encoding="utf-8",
    )
    environment.write_text("# test environment\n", encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    config = HubConfig.load()

    assert config.api.port == 8443
    assert config.mdns.enabled is False
    assert config.persistence.adapter == "sqlite"
    assert config.persistence.sqlite_path == "runtime/test-hub.sqlite3"
    assert config.persistence.directory_cache_enabled is False
