"""SPEC-M13 §D: the new DigitalHub/Duplex multi-document corpora
(generate_digitalhub_corpus.py / generate_duplex_corpus.py) -- CI-safe,
no live Azure AI Search call (that evidence is
docs/reports/2026-09-16-m13-dataset-pack-retrieval-baseline.md, per this
project's established split between CI-safe unit tests and a live-data
baseline report, matching tests/test_azure_ai_search_retrieval.py's own
header).

Every test drives the real, production `AgentService.invoke()` path
against a `ProjectResources` built directly (not via
`ServiceContainer.get_project`, which requires ADLS to resolve a non-
"demo" project id) -- the same technique
tests/test_azure_ai_search_retrieval.py's `_non_demo_project` helper
uses, so `execution_metadata`/audit events carry the real project id
these fixtures belong to, not a borrowed "demo" label.
"""

from __future__ import annotations

from pathlib import Path

from app.agent.graph import AgentService
from app.config import Settings
from app.services import ProjectResources, ServiceContainer, SourceManifest
from app.tools.document.analyzer import DocumentAnalyzer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
DUPLEX_DIR = ROOT / "demo_data" / "projects" / "duplex"
DIGITALHUB_DIR = ROOT / "demo_data" / "projects" / "digitalhub"


def _project(tmp_path: Path, project_id: str, data_dir: Path, ifc_file: str, pdf_files: list[str]) -> tuple[ServiceContainer, AgentService, ProjectResources]:
    settings = Settings(
        data_dir=data_dir, ifc_file=ifc_file, pdf_files=pdf_files,
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    fake_model = FakeModelProvider()
    container = ServiceContainer(
        settings,
        text_provider_factory=lambda s: fake_model, vision_provider_factory=lambda s: fake_model,
        search_client_factory=lambda s: None, embedding_provider_factory=lambda s: None,
    )
    service = AgentService(container)
    manifest = SourceManifest(
        project_id=project_id, display_name=project_id, source_set_id=f"{project_id}-test",
        ifc_file=ifc_file, pdf_files=pdf_files, file_hashes={},
    )
    resources = ProjectResources(container.ifc_repository, container.document_analyzers, manifest)
    return container, service, resources


def test_duplex_schedule_tables_are_readable_via_native_extraction() -> None:
    for name, expected_columns, expected_rows in [
        ("duplex_schedule_level1.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("duplex_schedule_level2.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("duplex_schedule_roof.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("duplex_door_spec_sheet.pdf", ["Item", "Fire Rating", "Frame Material"], 3),
        ("duplex_window_spec_sheet.pdf", ["Item", "Glazing Type", "Frame Material"], 3),
    ]:
        analyzer = DocumentAnalyzer(pdf_path=DUPLEX_DIR / "corpus" / name, evidence_dir=Path("/tmp"))
        table = analyzer._read_table(1)
        assert table is not None, f"{name} should have a readable table"
        assert [column.label for column in table.columns] == expected_columns
        assert len(table.rows) == expected_rows


def test_digitalhub_schedule_tables_are_readable_via_native_extraction() -> None:
    for name, expected_columns, expected_rows in [
        ("digitalhub_schedule_b01.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("digitalhub_schedule_e00.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("digitalhub_schedule_e01.pdf", ["Board", "Connected Load (kW)", "Diversity Factor"], 2),
        ("digitalhub_door_spec_sheet.pdf", ["Item", "Fire Rating", "Frame Material"], 3),
        ("digitalhub_window_spec_sheet.pdf", ["Item", "Glazing Type", "Frame Material"], 3),
    ]:
        analyzer = DocumentAnalyzer(pdf_path=DIGITALHUB_DIR / "corpus" / name, evidence_dir=Path("/tmp"))
        table = analyzer._read_table(1)
        assert table is not None, f"{name} should have a readable table"
        assert [column.label for column in table.columns] == expected_columns
        assert len(table.rows) == expected_rows


def test_duplex_and_digitalhub_narrative_documents_have_no_table() -> None:
    for path in [
        DUPLEX_DIR / "corpus" / "duplex_rfi_log_001.pdf",
        DUPLEX_DIR / "corpus" / "duplex_meeting_minutes_2026_02_10.pdf",
        DIGITALHUB_DIR / "corpus" / "digitalhub_rfi_log_001.pdf",
        DIGITALHUB_DIR / "corpus" / "digitalhub_meeting_minutes_2026_02_10.pdf",
    ]:
        analyzer = DocumentAnalyzer(pdf_path=path, evidence_dir=Path("/tmp"))
        assert analyzer._read_table(1) is None, f"{path.name} should have no readable table"


def test_duplex_panel_a_collision_is_a_zero_model_call_clarification(tmp_path: Path) -> None:
    """Mirrors SPEC-M6's own proven precision-collision test exactly
    (tests/test_azure_ai_search_retrieval.py's
    `..._collision_case_is_unaffected...`): "Panel-A" appears in both
    duplex_schedule_level1.pdf and duplex_schedule_level2.pdf with
    different values -- native_lookup alone must find both hits and
    return an honest, zero-model-call clarification naming both
    documents, never guess between them.
    """
    _, service, resources = _project(
        tmp_path, "duplex", DUPLEX_DIR, "Duplex_A_20110907.ifc",
        ["corpus/duplex_schedule_level1.pdf", "corpus/duplex_schedule_level2.pdf"],
    )

    response = service.invoke(project_resources=resources, thread_id="duplex-collision", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "clarification_required"
    assert "duplex_schedule_level1.pdf" in response.answer_markdown and "duplex_schedule_level2.pdf" in response.answer_markdown
    assert response.execution_metadata.get("model_call_count", 0) == 0


def test_digitalhub_panel_a_collision_is_a_zero_model_call_clarification(tmp_path: Path) -> None:
    _, service, resources = _project(
        tmp_path, "digitalhub", DIGITALHUB_DIR, "DigitalHub_FM-ARC_v2.ifc",
        ["corpus/digitalhub_schedule_b01.pdf", "corpus/digitalhub_schedule_e00.pdf"],
    )

    response = service.invoke(project_resources=resources, thread_id="digitalhub-collision", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "clarification_required"
    assert "digitalhub_schedule_b01.pdf" in response.answer_markdown and "digitalhub_schedule_e00.pdf" in response.answer_markdown
    assert response.execution_metadata.get("model_call_count", 0) == 0


def test_duplex_unambiguous_panel_is_answered_deterministically(tmp_path: Path) -> None:
    """The non-colliding label on the same document set resolves cleanly
    -- proves the collision test above is a genuine ambiguity finding,
    not an artifact of every question on this corpus being unanswerable.
    """
    _, service, resources = _project(
        tmp_path, "duplex", DUPLEX_DIR, "Duplex_A_20110907.ifc",
        ["corpus/duplex_schedule_level1.pdf", "corpus/duplex_schedule_level2.pdf"],
    )

    response = service.invoke(project_resources=resources, thread_id="duplex-unambiguous", viewer_context=None, question="What is the connected load for Panel-C?")

    assert response.disposition.value == "answered"
    assert "14.10" in response.answer_markdown
    assert response.execution_metadata.get("model_call_count", 0) == 0
