"""Composition-owned OpenTelemetry trace, metric, and log export."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode


class _ExporterLogFilter(logging.Filter):
    """Prevent exporter diagnostics from recursively entering OTLP logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(("opentelemetry.exporter", "grpc"))


@dataclass(slots=True)
class OpenTelemetryRuntime:
    enabled: bool
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    logging_handler: LoggingHandler | None = None

    def shutdown(self) -> None:
        if self.logging_handler is not None:
            logging.getLogger().removeHandler(self.logging_handler)
        if self.logger_provider is not None:
            self.logger_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


def configure_opentelemetry(
    *,
    enabled: bool,
    service_name: str,
    endpoint: str,
) -> OpenTelemetryRuntime:
    """Build local providers without mutating process-global OTEL providers."""

    if not enabled or not endpoint:
        return OpenTelemetryRuntime(enabled=False)

    resource = Resource.create({SERVICE_NAME: service_name})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=(metric_reader,),
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
    )
    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(_ExporterLogFilter())
    logging.getLogger().addHandler(handler)
    return OpenTelemetryRuntime(
        enabled=True,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        logging_handler=handler,
    )


class OpenTelemetryHttpMiddleware:
    """ASGI HTTP telemetry that deliberately excludes request data and secrets."""

    def __init__(
        self,
        app,
        *,
        runtime: Callable[[], OpenTelemetryRuntime | None],
    ) -> None:
        self._app = app
        self._runtime = runtime
        self._instrument_runtime: OpenTelemetryRuntime | None = None
        self._requests = None
        self._duration = None

    async def __call__(self, scope, receive, send) -> None:
        runtime = self._runtime()
        if scope["type"] != "http" or runtime is None or not runtime.enabled:
            await self._app(scope, receive, send)
            return
        assert runtime.tracer_provider is not None
        assert runtime.meter_provider is not None
        tracer = runtime.tracer_provider.get_tracer("eidolon_hub.http")
        if runtime is not self._instrument_runtime:
            meter = runtime.meter_provider.get_meter("eidolon_hub.http")
            self._requests = meter.create_counter(
                "eidolon.hub.http.requests",
                description="Completed Hub HTTP requests",
            )
            self._duration = meter.create_histogram(
                "eidolon.hub.http.duration",
                unit="ms",
                description="Hub HTTP request duration",
            )
            self._instrument_runtime = runtime
        assert self._requests is not None
        assert self._duration is not None
        started = time.perf_counter()
        method = str(scope.get("method") or "")
        status_code = 500

        async def observe_send(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        with tracer.start_as_current_span("HTTP request") as span:
            try:
                await self._app(scope, receive, observe_send)
            except BaseException as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                route = scope.get("route")
                route_template = str(getattr(route, "path", "unmatched"))
                attributes = {
                    "http.request.method": method,
                    "http.route": route_template,
                    "http.response.status_code": status_code,
                }
                for key, value in attributes.items():
                    span.set_attribute(key, value)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                self._requests.add(1, attributes)
                self._duration.record(
                    (time.perf_counter() - started) * 1_000,
                    attributes,
                )
