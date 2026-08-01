from __future__ import annotations

from hub.adapters.observability.opentelemetry import configure_opentelemetry
from hub.config import _observability_from_yaml


def test_telemetry_export_is_disabled_without_endpoint() -> None:
    runtime = configure_opentelemetry(
        enabled=True,
        service_name="eidolon-hub-test",
        endpoint="",
    )

    assert runtime.enabled is False
    assert runtime.tracer_provider is None
    assert runtime.meter_provider is None
    assert runtime.logger_provider is None
    runtime.shutdown()


def test_standard_otlp_endpoint_environment_overrides_yaml(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")

    config = _observability_from_yaml(
        {
            "observability": {
                "enabled": True,
                "service_name": "eidolon-hub-local",
                "otlp_endpoint": "http://ignored:4317",
            }
        }
    )

    assert config.enabled is True
    assert config.service_name == "eidolon-hub-local"
    assert config.otlp_endpoint == "http://collector:4317"
