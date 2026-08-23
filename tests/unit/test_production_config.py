from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import hub.config as config_module
from hub.config import (
    ChannelProviderConfig,
    HubConfig,
    OnboardingConfig,
    PersistenceConfig,
)


def test_default_config_knows_only_local_behavior_and_public_contract_addresses(
    tmp_path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("EIDOLON_STATE_ROOT", str(state_root))
    config = HubConfig()

    assert config.channel_provider.contract_url == "http://127.0.0.1:8767/v1"
    assert config.onboarding.owner_domain_id == "owner-local"
    assert config.onboarding.descriptor_uri == (
        "https://eidolon-hub.local/api/device-onboarding/v1/descriptor"
    )
    assert config.persistence.path == str(state_root / "hub/eidolon-hub.sqlite3")
    assert not hasattr(config, "deployment")
    assert not hasattr(config, "api")
    assert not hasattr(config, "channel_control")


def test_onboarding_requires_https() -> None:
    with pytest.raises(ValidationError, match="plain HTTPS descriptor URI"):
        OnboardingConfig(
            descriptor_uri="http://hub.example/api/device-onboarding/v1/descriptor"
        )


@pytest.mark.parametrize("seconds", (59, 86_401))
def test_retrieval_window_is_bounded(seconds) -> None:
    with pytest.raises(ValidationError):
        OnboardingConfig(retrieval_window_seconds=seconds)


def test_channel_provider_contract_address_is_strictly_bounded() -> None:
    with pytest.raises(ValidationError, match=r"plain HTTP\(S\) base URL"):
        ChannelProviderConfig(contract_url="https://user:secret@provider.example/v1?redirect=evil")


def test_remote_channel_provider_requires_https() -> None:
    with pytest.raises(ValidationError, match="remote Channel Provider must use HTTPS"):
        ChannelProviderConfig(contract_url="http://provider.example/v1")


def test_mdns_allows_a_publicly_trusted_https_hostname() -> None:
    config = HubConfig(
        onboarding=OnboardingConfig(
            descriptor_uri=(
                "https://hub.example.com/api/device-onboarding/v1/descriptor"
            )
        ),
    )
    assert config.discovery.mdns.enabled is True


def test_checked_in_settings_loads_as_the_only_configuration(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)
    monkeypatch.setenv("EIDOLON_STATE_ROOT", str(state_root))

    config = HubConfig.load()

    assert config.persistence == PersistenceConfig(
        path=str(state_root / "hub/eidolon-hub.sqlite3"),
        authority_anchor_path=str(state_root / "hub/authority-lineage.json"),
        authority_bootstrap_path=str(state_root / "hub/authority-bootstrap.json"),
    )
    assert config.discovery.mdns.enabled is True


def test_explicit_settings_path_is_the_simple_override(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "custom.yaml"
    settings.write_text(
        """
onboarding:
  descriptor_uri: https://custom.local/api/device-onboarding/v1/descriptor
channel_provider:
  contract_url: https://provider.example/v1
persistence:
  path: /tmp/eidolon-hub-custom.sqlite3
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))
    monkeypatch.delenv("EIDOLON_HUB_ENV_FILE", raising=False)

    config = HubConfig.load()

    assert config.onboarding.descriptor_uri == (
        "https://custom.local/api/device-onboarding/v1/descriptor"
    )
    assert config.channel_provider.contract_url == "https://provider.example/v1"
    assert config.persistence.path == "/tmp/eidolon-hub-custom.sqlite3"


def test_settings_path_expands_host_contract_environment(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "persistence:\n  path: $EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EIDOLON_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))

    config = HubConfig.load()

    assert config.persistence.path == str(state_root / "hub/eidolon-hub.sqlite3")


def test_installed_wheel_falls_back_to_packaged_settings(tmp_path, monkeypatch) -> None:
    installed = tmp_path / "installed" / "config"
    installed.mkdir(parents=True)
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    (installed / "settings.yaml").write_text(
        canonical.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "_CONFIG_ROOT", tmp_path / "missing")
    monkeypatch.setattr(config_module, "_INSTALLED_CONFIG_ROOT", installed)
    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)

    assert HubConfig.load().persistence.path.endswith("/eidolon-hub.sqlite3")


def test_explicit_missing_dotenv_still_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_HUB_ENV_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(FileNotFoundError, match="environment file is missing"):
        HubConfig.load()


@pytest.mark.parametrize(
    "retired_fragment, expected",
    (
        ("\ndeployment:\n  mode: cloud\n", "deployment"),
        (
            "\npersistence:\n  adapter: postgresql\n  dsn: postgresql://db/eidolon_hub\n",
            "persistence",
        ),
        ("\nconnection_plane: {}\n", "connection_plane"),
    ),
)
def test_retired_cloud_or_misspelled_settings_fail_closed(
    retired_fragment, expected, tmp_path, monkeypatch
) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    settings = tmp_path / "settings.yaml"
    base = canonical.read_text(encoding="utf-8")
    if retired_fragment.startswith("\npersistence:"):
        base = base.split("\npersistence:", 1)[0]
    settings.write_text(base + retired_fragment, encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))

    with pytest.raises(ValidationError, match=expected):
        HubConfig.load()


def test_yaml_types_are_strict(tmp_path, monkeypatch) -> None:
    canonical = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    settings = tmp_path / "settings.yaml"
    value = canonical.read_text(encoding="utf-8").replace(
        "    enabled: true",
        '    enabled: "true"',
    )
    settings.write_text(value, encoding="utf-8")
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(settings))

    with pytest.raises(ValidationError, match="bool_type"):
        HubConfig.load()
