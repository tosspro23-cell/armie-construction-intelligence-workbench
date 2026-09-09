"""OpenTelemetry wiring tests (SPEC-M3 §8).

The ``wire`` injection seam is used in every test so a real OpenTelemetry
TracerProvider/AzureMonitorTraceExporter is never constructed here: doing so
would start a background export thread that attempts network I/O even with
a throwaway instrumentation key, violating this repository's documented
no-network-egress CI policy (.github/workflows/ci.yml). This mirrors the
provider-factory injection discipline already established for the
probabilistic path (D-007) -- the seam is tested directly, not bypassed.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.telemetry import configure_telemetry
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, otel_exporter_connection_string: str | None) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf",
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
        otel_exporter_connection_string=otel_exporter_connection_string,
    )
    settings.ensure_runtime_directories()
    return settings


def test_configure_telemetry_is_a_no_op_without_a_connection_string(tmp_path: Path) -> None:
    app = FastAPI()
    settings = _settings(tmp_path, None)
    calls = []

    configure_telemetry(app, settings, wire=lambda a, c: calls.append((a, c)))

    assert calls == []
    assert getattr(app.state, "otel_configured", False) is False


def test_configure_telemetry_wires_when_a_connection_string_is_set(tmp_path: Path) -> None:
    app = FastAPI()
    connection_string = "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    settings = _settings(tmp_path, connection_string)
    calls = []

    configure_telemetry(app, settings, wire=lambda a, c: calls.append((a, c)))

    assert calls == [(app, connection_string)]
    assert app.state.otel_configured is True


def test_configure_telemetry_does_not_wire_twice(tmp_path: Path) -> None:
    app = FastAPI()
    settings = _settings(tmp_path, "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    calls = []

    configure_telemetry(app, settings, wire=lambda a, c: calls.append((a, c)))
    configure_telemetry(app, settings, wire=lambda a, c: calls.append((a, c)))

    assert len(calls) == 1
