from __future__ import annotations

import pytest

from hub.adapters.observability.opentelemetry import configure_opentelemetry
from hub.composition.resources import load_runtime_environment
from hub.config import HubConfig, ObservabilityConfig


def test_telemetry_export_is_explicitly_disabled() -> None:
    runtime = configure_opentelemetry(
        enabled=False,
        service_name="eidolon-hub-test",
        endpoint="",
    )

    assert runtime.enabled is False
    assert runtime.tracer_provider is None
    assert runtime.meter_provider is None
    assert runtime.logger_provider is None
    runtime.shutdown()


def test_enabled_telemetry_requires_standard_otlp_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    config = HubConfig(observability=ObservabilityConfig(enabled=True))

    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        load_runtime_environment(config)


def test_standard_otlp_endpoint_is_loaded_from_runtime_environment(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    config = HubConfig(observability=ObservabilityConfig(enabled=True))

    environment = load_runtime_environment(config)

    assert environment.otlp_endpoint == "http://collector:4317"


def test_invalid_otlp_endpoint_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")

    with pytest.raises(RuntimeError, match=r"HTTP\(S\) endpoint"):
        load_runtime_environment(HubConfig(observability=ObservabilityConfig(enabled=True)))
