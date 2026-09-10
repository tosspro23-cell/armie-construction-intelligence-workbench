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

import subprocess
import sys
from pathlib import Path

from app.config import Settings
from app.telemetry import configure_telemetry
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
REPRO_SCRIPT = Path(__file__).resolve().parent / "_fastapi_instrumentation_timing_repro.py"


def _settings(tmp_path: Path, otel_exporter_connection_string: str | None) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
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


def test_instrumenting_inside_lifespan_produces_no_spans_but_matching_main_py_does() -> None:
    """Independent review finding, reproduced directly: FastAPIInstrumentor
    exported zero spans when called from inside a ``lifespan`` context
    manager (how apps/api/app/main.py originally called it), and correctly
    exported a SERVER span when called right after ``FastAPI()``
    construction (how main.py calls it now). Each case runs in its own
    subprocess because OpenTelemetry's TracerProvider is a process-global
    singleton that can only be set once -- trying both cases in one process
    made the second one silently reuse the first's already-finalized
    provider and produced a false negative the first time this was tried.
    """
    lifespan_result = subprocess.run(
        [sys.executable, str(REPRO_SCRIPT), "lifespan"],
        capture_output=True, text=True, check=True,
    )
    eager_result = subprocess.run(
        [sys.executable, str(REPRO_SCRIPT), "eager"],
        capture_output=True, text=True, check=True,
    )

    assert int(lifespan_result.stdout.strip()) == 0
    assert int(eager_result.stdout.strip()) == 1
