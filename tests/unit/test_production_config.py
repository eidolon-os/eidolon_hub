from __future__ import annotations

from pathlib import Path

import pytest

from hub.config import (
    ChannelProviderConfig,
    DeviceAccessConfig,
    HubConfig,
    PersistenceConfig,
    validate_hub_config,
)


def test_default_production_config_knows_only_provider_contract_address() -> None:
    config = HubConfig()

    validate_hub_config(config)

    assert config.channel_provider.contract_url == "http://127.0.0.1:8090/v1"
    assert not hasattr(config, "channel_control")


def test_device_access_requires_https_and_a_bounded_session_lease() -> None:
    config = HubConfig(device_access=DeviceAccessConfig(public_base_url="http://hub.example"))

    with pytest.raises(ValueError, match="plain HTTPS base URL"):
        validate_hub_config(config)


def test_channel_provider_contract_address_is_strictly_bounded() -> None:
    config = HubConfig(
        channel_provider=ChannelProviderConfig(
            contract_url="https://user:secret@provider.example/v1?redirect=evil"
        )
    )

    with pytest.raises(ValueError, match=r"plain HTTP\(S\) base URL"):
        validate_hub_config(config)


def test_remote_channel_provider_requires_https() -> None:
    config = HubConfig(
        channel_provider=ChannelProviderConfig(contract_url="http://provider.example/v1")
    )

    with pytest.raises(ValueError, match="remote Channel Provider must use HTTPS"):
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
persistence:
  adapter: sqlite
  sqlite_path: runtime/test-hub.sqlite3
  directory_cache_enabled: false
device_access:
  public_base_url: https://hub.example
discovery:
  mdns: {enabled: false}
channel_provider:
  contract_url: https://provider.example/v1
""".strip(),
        encoding="utf-8",
    )
    environment.write_text("# test environment\n", encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    config = HubConfig.load()

    assert config.api.port == 8443
    assert config.discovery.mdns.enabled is False
    assert config.persistence.adapter == "sqlite"
    assert config.persistence.sqlite_path == "runtime/test-hub.sqlite3"
    assert config.persistence.directory_cache_enabled is False
    assert config.channel_provider.contract_url == "https://provider.example/v1"


def test_checked_in_settings_match_the_complete_setting_model(tmp_path, monkeypatch) -> None:
    environment = tmp_path / ".env"
    environment.write_text("# no secrets required while parsing settings\n", encoding="utf-8")
    settings = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    config = HubConfig.load()

    assert config.discovery.mdns.enabled is True
    assert config.persistence.adapter == "sqlite"
    assert not hasattr(config, "mdns")
    assert not hasattr(config.device_access, "explicit_descriptor_uris")


@pytest.mark.parametrize(
    "retired_setting",
    (
        "\nlogging: {level: DEBUG}\n",
        "\nmdns: {enabled: true}\n",
    ),
)
def test_retired_top_level_settings_fail_closed(retired_setting, tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    settings = tmp_path / "settings.yaml"
    environment = tmp_path / ".env"
    settings.write_text(canonical.read_text(encoding="utf-8") + retired_setting, encoding="utf-8")
    environment.write_text("# test environment\n", encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    with pytest.raises(ValueError, match="unknown Hub settings at root"):
        HubConfig.load()


def test_retired_connection_plane_fails_closed(tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    settings = tmp_path / "settings.yaml"
    environment = tmp_path / ".env"
    value = canonical.read_text(encoding="utf-8") + "\nconnection_plane: {}\n"
    settings.write_text(value, encoding="utf-8")
    environment.write_text("# test environment\n", encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    with pytest.raises(ValueError, match="unknown Hub settings at root"):
        HubConfig.load()


def test_yaml_booleans_are_strictly_typed(tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    settings = tmp_path / "settings.yaml"
    environment = tmp_path / ".env"
    value = canonical.read_text(encoding="utf-8").replace(
        "    enabled: true\n    service_type:",
        '    enabled: "true"\n    service_type:',
    )
    settings.write_text(value, encoding="utf-8")
    environment.write_text("# test environment\n", encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(environment))

    with pytest.raises(ValueError, match="discovery.mdns.enabled must be a YAML boolean"):
        HubConfig.load()
