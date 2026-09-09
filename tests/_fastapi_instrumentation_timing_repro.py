"""Standalone script, run in its own subprocess by
test_telemetry.py::test_instrumenting_inside_lifespan_produces_no_spans_but_eager_does.

OpenTelemetry's TracerProvider is a process-global singleton (the SDK
refuses a second ``set_tracer_provider`` call), so the two cases this
compares -- instrumenting FastAPIInstrumentor from inside a ``lifespan``
context manager (the pre-fix bug) vs. right after ``FastAPI()`` construction
(the fix actually landed in apps/api/app/main.py) -- cannot be exercised
side by side in one process without the second case silently reusing the
first case's (already-finalized) provider and producing a false result.
Each invocation of this script is one isolated case.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

mode = sys.argv[1]  # "lifespan" (the bug) or "eager" (the fix, matches main.py)

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

if mode == "lifespan":
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        FastAPIInstrumentor.instrument_app(app)
        yield
    app = FastAPI(lifespan=lifespan)
else:
    app = FastAPI()
    FastAPIInstrumentor.instrument_app(app)

@app.get("/health")
def health():
    return {"status": "ok"}

with TestClient(app) as client:
    response = client.get("/health")
    assert response.status_code == 200, response.status_code

server_spans = [s for s in exporter.get_finished_spans() if str(s.kind) == "SpanKind.SERVER"]
print(len(server_spans))
