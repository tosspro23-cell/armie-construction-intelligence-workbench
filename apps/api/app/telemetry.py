from __future__ import annotations

from typing import Callable

from fastapi import FastAPI

from app.config import Settings

WireFn = Callable[[FastAPI, str], None]


def configure_telemetry(app: FastAPI, settings: Settings, *, wire: WireFn | None = None) -> None:
    """Wire OpenTelemetry -> Application Insights, opt-in only (SPEC-M3 §4D).

    A no-op unless ``otel_exporter_connection_string`` is set -- the same
    inert-by-default pattern as ``ollama_escalation_model`` (config.py).
    This adds transport only: it must never change
    ``/api/v1/traces/{trace_id}``, ``AuditStore``, or any existing audit
    event shape (SPEC-M3 §7). ``wire`` is an injection seam for tests, so a
    real OpenTelemetry TracerProvider/exporter is never constructed outside
    of an actual configured deployment -- constructing one starts a
    background export thread, which would violate this repository's
    documented no-network-egress CI policy (.github/workflows/ci.yml) even
    with a throwaway instrumentation key.
    """
    if not settings.otel_exporter_connection_string:
        return
    if getattr(app.state, "otel_configured", False):
        return
    (wire or _wire_azure_monitor)(app, settings.otel_exporter_connection_string)
    app.state.otel_configured = True


def _wire_azure_monitor(app: FastAPI, connection_string: str) -> None:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    from opentelemetry import trace
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": "armie-construction-intelligence-workbench-api"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=connection_string)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
