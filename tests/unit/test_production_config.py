from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import hub.config as config_module
from hub.config import (
    ChannelProviderConfig,
    DeploymentConfig,
    DeviceAccessConfig,
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    PostgresqlPersistenceConfig,
)


def test_default_config_knows_only_public_contract_addresses() -> None:
    config = HubConfig()

    assert config.channel_provider.contract_url == "http://127.0.0.1:8090/v1"
    assert config.device_access.public_base_url == "https://eidolon-hub.local"
    assert not hasattr(config, "api")
    assert not hasattr(config, "channel_control")


def test_device_access_requires_https() -> None:
    with pytest.raises(ValidationError, match="plain HTTPS base URL"):
        DeviceAccessConfig(public_base_url="http://hub.example")


def test_heartbeat_must_leave_three_attempts_before_session_expiry() -> None:
    with pytest.raises(ValidationError, match="one third"):
        DeviceAccessConfig(session_lease_seconds=30, heartbeat_after_ms=15_000)


def test_channel_provider_contract_address_is_strictly_bounded() -> None:
    with pytest.raises(ValidationError, match=r"plain HTTP\(S\) base URL"):
        ChannelProviderConfig(contract_url="https://user:secret@provider.example/v1?redirect=evil")


def test_remote_channel_provider_requires_https() -> None:
    with pytest.raises(ValidationError, match="remote Channel Provider must use HTTPS"):
        ChannelProviderConfig(contract_url="http://provider.example/v1")


def test_cloud_requires_postgresql_and_disables_mdns() -> None:
    with pytest.raises(ValidationError, match="cloud deployment requires PostgreSQL"):
        HubConfig(
            deployment=DeploymentConfig(mode="cloud"),
            discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
        )

    with pytest.raises(ValidationError, match="cloud deployment cannot enable"):
        HubConfig(
            deployment=DeploymentConfig(mode="cloud"),
            persistence=PostgresqlPersistenceConfig(),
            device_access=DeviceAccessConfig(public_base_url="https://hub.example.com"),
        )


def test_postgresql_dsn_contains_target_but_rejects_credentials() -> None:
    config = PostgresqlPersistenceConfig(
        dsn="postgresql://db.internal:5432/eidolon_hub?ssl=require",
        pool_timeout_seconds=7,
        pool_recycle_seconds=900,
    )

    assert config.dsn.endswith("/eidolon_hub?ssl=require")
    assert config.pool_timeout_seconds == 7
    assert config.pool_recycle_seconds == 900

    with pytest.raises(ValidationError, match="credential-free"):
        PostgresqlPersistenceConfig(dsn="postgresql://user:secret@db.internal/eidolon_hub")
    with pytest.raises(ValidationError, match="credential-free"):
        PostgresqlPersistenceConfig(dsn="postgresql://db.internal/eidolon_hub?password=secret")


def test_profile_selector_loads_checked_in_local_and_cloud_settings(monkeypatch) -> None:
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)

    monkeypatch.setenv("EIDOLON_HUB_PROFILE", "local")
    local = HubConfig.load()
    monkeypatch.setenv("EIDOLON_HUB_PROFILE", "cloud")
    cloud = HubConfig.load()

    assert local.deployment.mode == "local"
    assert local.persistence.adapter == "sqlite"
    assert local.discovery.mdns.enabled is True
    assert cloud.deployment.mode == "cloud"
    assert cloud.persistence.adapter == "postgresql"
    assert cloud.discovery.mdns.enabled is False


def test_explicit_settings_path_is_the_simple_override(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "custom.yaml"
    settings.write_text(
        """
deployment:
  mode: local
device_access:
  public_base_url: https://custom.local
channel_provider:
  contract_url: https://provider.example/v1
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)

    config = HubConfig.load()

    assert config.device_access.public_base_url == "https://custom.local"
    assert config.channel_provider.contract_url == "https://provider.example/v1"


def test_invalid_profile_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)
    monkeypatch.setenv("EIDOLON_HUB_PROFILE", "staging")

    with pytest.raises(ValueError, match="must be local or cloud"):
        HubConfig.load()


def test_cloud_does_not_require_dotenv_file(monkeypatch) -> None:
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)
    monkeypatch.setenv("EIDOLON_HUB_PROFILE", "cloud")

    assert HubConfig.load().deployment.mode == "cloud"


def test_installed_wheel_falls_back_to_packaged_profile_data(tmp_path, monkeypatch) -> None:
    installed = tmp_path / "installed" / "config"
    installed.mkdir(parents=True)
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.local.yaml"
    (installed / "settings.local.yaml").write_text(
        canonical.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "_CONFIG_ROOT", tmp_path / "missing")
    monkeypatch.setattr(config_module, "_INSTALLED_CONFIG_ROOT", installed)
    monkeypatch.setenv("EIDOLON_HUB_PROFILE", "local")
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)

    assert HubConfig.load().deployment.mode == "local"


def test_explicit_missing_dotenv_still_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(FileNotFoundError, match="environment file is missing"):
        HubConfig.load()


@pytest.mark.parametrize("profile", ("local", "cloud"))
def test_checked_in_profiles_match_complete_strict_model(profile, monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_HUB_PROFILE", profile)
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)

    assert HubConfig.load().deployment.mode == profile


def test_retired_or_misspelled_settings_fail_closed(tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.local.yaml"
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        canonical.read_text(encoding="utf-8") + "\nconnection_plane: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))

    with pytest.raises(ValidationError, match="connection_plane"):
        HubConfig.load()


def test_yaml_types_are_strict(tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.local.yaml"
    settings = tmp_path / "settings.yaml"
    value = canonical.read_text(encoding="utf-8").replace(
        "    enabled: true",
        '    enabled: "true"',
    )
    settings.write_text(value, encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))

    with pytest.raises(ValidationError, match="bool_type"):
        HubConfig.load()
