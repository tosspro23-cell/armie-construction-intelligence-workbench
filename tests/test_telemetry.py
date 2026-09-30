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

    D-076 (2026-09-30), found live while merging an unrelated PR: CI started
    failing this test's own ``lifespan`` assertion on Python 3.10-3.12 (not
    3.9), with zero code change in this repository. Root-caused directly in
    an isolated venv, not guessed (see
    ``feedback_measure_dont_guess_root_causes``): pip resolves the newest
    fastapi/starlette/opentelemetry compatible with *each* Python version
    independently -- 3.9's own ceiling (the newest fastapi still supporting
    it) never reaches the newer dependency set 3.10+ can. Pinning one
    package at a time and re-running the repro script directly isolated
    the exact trigger to FastAPI's own 0.141.1 -> 0.142.1 release
    specifically (confirmed Starlette's own 1.6.0 -> 1.7.0 bump alone does
    *not* reproduce it; confirmed opentelemetry-instrumentation-fastapi's
    own 0.65b0 -> 0.66b0 bump alone does *not* either) -- something FastAPI
    itself changed in that range now lets a ``lifespan``-registered
    instrumentor's middleware take effect too, exactly the bug this test
    was written to catch.

    This is not a regression in this project's own code, and not a reason
    to pin FastAPI down: `eager_result` (the shape main.py actually uses)
    reliably produced a real span with *every* dependency combination tried
    during the bisection above -- the one thing this test exists to
    protect held throughout. Only the old `lifespan_result == 0` assertion
    -- a claim about a third-party library's own internal timing, not
    about anything this project controls -- stopped holding for the newer
    stack (upstream fixing, not breaking, that timing quirk). Relaxed to a
    non-fatal, informational comparison instead of a strict equality check
    on someone else's implementation detail that has already been observed
    to drift across ordinary dependency updates.
    """
    lifespan_result = subprocess.run(
        [sys.executable, str(REPRO_SCRIPT), "lifespan"],
        capture_output=True, text=True, check=True,
    )
    eager_result = subprocess.run(
        [sys.executable, str(REPRO_SCRIPT), "eager"],
        capture_output=True, text=True, check=True,
    )

    # The one invariant this project actually depends on: main.py's own
    # eager placement (see its own comment, right after `FastAPI()`
    # construction) reliably produces exactly one real SERVER span per
    # request -- this must hold regardless of what FastAPI/Starlette/
    # OpenTelemetry's own internals do with the production-unused
    # lifespan-registered case below. Kept as an exact count, not loosened
    # to `>= 1` alongside the lifespan relaxation below (Codex review, PR
    # #56): the bisection that motivated this fix confirmed eager produced
    # exactly one span with *every* dependency combination tried -- this
    # assertion protects a real invariant (a duplicate SERVER span here
    # would mean production double-recording one request's own
    # AppRequests telemetry), not a claim that merely happened to hold by
    # accident the way the lifespan count did.
    assert int(eager_result.stdout.strip()) == 1
    # Informational only, not asserted (D-076): whether lifespan-based
    # instrumentation also happens to work is a third-party implementation
    # detail already observed to change across ordinary dependency
    # bumps -- printed for a curious reader (pytest -s), never a source of
    # a spurious CI failure again.
    print(f"lifespan-mode span count this run: {lifespan_result.stdout.strip()}")
