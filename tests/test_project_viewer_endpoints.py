"""GET /api/v1/project/{metadata,pdf/pages/{n}.png,viewer-elements}.

Independent-review finding (P2 #10), confirmed live 2026-09-13: SPEC-M9
made these routes `async def` (needed for the `await
container.get_project(...)` call a cold ADLS-mode project load requires),
but each route's own CPU/file-bound work -- PDF text/page inspection, PDF
page rasterization, IFC geometry projection -- ran as a direct synchronous
call with no `await` of its own. A plain `def` FastAPI route gets Starlette's
own external-threadpool offload automatically; an `async def` route that
never awaits its blocking work runs it directly on the event loop instead,
blocking every other concurrent request (including health checks and
cancellation) for as long as it takes.

No test exercised any of these three endpoints at all before this fix --
found while fixing #10, not assumed. Covers both baseline correctness and
the actual claim itself: each blocking call now genuinely runs off the
event loop's own thread. A wall-clock race between two concurrent
requests was tried first and rejected -- it passed even against the
pre-fix code (an `await _resolve_project(...)` earlier in the same
route apparently yields enough for asyncio's scheduler to interleave a
short concurrent request anyway, so timing alone did not actually
distinguish the two states) -- a direct thread-identity check does not
depend on scheduling timing at all.
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.config import get_settings
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()


def test_project_metadata_returns_real_ifc_and_pdf_inspection(monkeypatch, tmp_path):
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/project/metadata")

    assert response.status_code == 200
    body = response.json()
    assert body["ifc_available"] is True
    assert body["pdf_available"] is True
    assert len(body["ifc"]["storeys"]) >= 1
    assert body["pdf"]["page_count"] >= 1


def test_project_pdf_page_renders_a_real_png(monkeypatch, tmp_path):
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/project/pdf/pages/1.png")

    assert response.status_code == 200
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_project_viewer_elements_returns_real_geometry(monkeypatch, tmp_path):
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/project/viewer-elements")

    assert response.status_code == 200
    body = response.json()
    assert body["representation"] == "ifcopenshell_bounding_geometry"
    assert len(body["elements"]) > 0


def _patch_to_record_event_loop_thread(monkeypatch) -> list[int]:
    """Records the thread `_resolve_project` actually runs on for the
    request about to be made -- straight-line `async def` code with no
    thread offload, so this is the event loop's own thread, the correct
    baseline to compare a blocking call's thread against. TestClient runs
    the whole ASGI app (including the event loop) on its own background
    "portal" thread, never the pytest thread the test body itself runs
    on -- comparing against the *test's* thread instead would make every
    assertion here pass trivially, fixed or not, since neither the event
    loop nor a to_thread worker is ever the pytest thread.
    """
    import app.main as main_module

    event_loop_thread_ids: list[int] = []
    real_resolve_project = main_module._resolve_project

    async def recording_resolve_project(project_id: str):
        event_loop_thread_ids.append(threading.get_ident())
        return await real_resolve_project(project_id)

    monkeypatch.setattr(main_module, "_resolve_project", recording_resolve_project)
    return event_loop_thread_ids


def test_project_pdf_page_render_runs_off_the_event_loops_own_thread(monkeypatch, tmp_path):
    """The actual claim, checked directly rather than inferred from
    timing: records which thread actually executes render_page and
    asserts it differs from the event loop's own thread for this same
    request. Before this fix, render_page ran inline on the event loop's
    own thread -- the same thread every other concurrent request, health
    check, and cancellation also needs to run on in the real deployed app.
    """
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module
    from app.tools.document.analyzer import DocumentAnalyzer

    event_loop_thread_ids = _patch_to_record_event_loop_thread(monkeypatch)
    render_thread_id: int | None = None
    real_render_page = DocumentAnalyzer.render_page

    def recording_render_page(self, *args, **kwargs):
        nonlocal render_thread_id
        render_thread_id = threading.get_ident()
        return real_render_page(self, *args, **kwargs)

    monkeypatch.setattr(DocumentAnalyzer, "render_page", recording_render_page)

    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/project/pdf/pages/1.png")

    assert response.status_code == 200
    assert render_thread_id is not None
    assert len(event_loop_thread_ids) == 1
    assert render_thread_id != event_loop_thread_ids[0]


def test_project_metadata_inspection_runs_off_the_event_loops_own_thread(monkeypatch, tmp_path):
    """Same claim as above, for project_metadata's two blocking calls
    (IfcRepository.metadata, DocumentAnalyzer.inspect)."""
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module
    from app.tools.document.analyzer import DocumentAnalyzer
    from app.tools.ifc.repository import IfcRepository

    event_loop_thread_ids = _patch_to_record_event_loop_thread(monkeypatch)
    recorded_thread_ids: list[int] = []
    real_ifc_metadata = IfcRepository.metadata
    real_pdf_inspect = DocumentAnalyzer.inspect

    def recording_ifc_metadata(self, *args, **kwargs):
        recorded_thread_ids.append(threading.get_ident())
        return real_ifc_metadata(self, *args, **kwargs)

    def recording_pdf_inspect(self, *args, **kwargs):
        recorded_thread_ids.append(threading.get_ident())
        return real_pdf_inspect(self, *args, **kwargs)

    monkeypatch.setattr(IfcRepository, "metadata", recording_ifc_metadata)
    monkeypatch.setattr(DocumentAnalyzer, "inspect", recording_pdf_inspect)

    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/project/metadata")

    assert response.status_code == 200
    assert len(recorded_thread_ids) == 2
    assert len(event_loop_thread_ids) == 1
    assert event_loop_thread_ids[0] not in recorded_thread_ids
