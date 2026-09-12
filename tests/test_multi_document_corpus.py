"""SPEC-M6: multi-document corpus, naive baseline, and its two evidenced
failure modes.

None of this corpus is in Settings.pdf_files' default (still just
armie_demo_schedule.pdf) -- every test here opts in explicitly, so the
existing single-document test suite (tests/test_pdf_deterministic_
extraction.py and friends) is unaffected and unmodified by this file.

No Ollama, no network, no downloaded model: FakeModelProvider has no
scripted response for any purpose, so an accidental model call in the
zero-model-call paths this file asserts raises AssertionError from the
fake itself rather than silently succeeding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


ROOT = Path(__file__).resolve().parents[1]
CORPUS = "corpus"

FULL_CORPUS = [
    "armie_demo_schedule.pdf",
    f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf",
    f"{CORPUS}/schedule_l3_east.pdf", f"{CORPUS}/schedule_l3_west.pdf",
    f"{CORPUS}/schedule_mezzanine.pdf", f"{CORPUS}/schedule_roof_plant.pdf",
    f"{CORPUS}/door_spec_sheet_package_a.pdf", f"{CORPUS}/door_spec_sheet_package_b.pdf",
    f"{CORPUS}/window_spec_sheet_package_a.pdf", f"{CORPUS}/window_spec_sheet_package_b.pdf",
    f"{CORPUS}/rfi_log_047.pdf", f"{CORPUS}/rfi_log_052.pdf", f"{CORPUS}/rfi_log_058.pdf", f"{CORPUS}/rfi_log_061.pdf",
    f"{CORPUS}/meeting_minutes_2026_02_10.pdf", f"{CORPUS}/meeting_minutes_2026_02_24.pdf", f"{CORPUS}/meeting_minutes_2026_03_10.pdf",
]


def _settings(tmp_path: Path, pdf_files: list[str]) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=pdf_files,
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    return settings


def _service(settings: Settings) -> tuple[ServiceContainer, AgentService]:
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    return container, AgentService(container)


# --- ServiceContainer plumbing (§A) -----------------------------------------------

def test_service_container_builds_one_analyzer_per_configured_document(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    container = ServiceContainer(settings)

    assert list(container.document_analyzers.keys()) == ["schedule_l2_east.pdf", "schedule_l2_west.pdf"]
    assert all(analyzer.available for analyzer in container.document_analyzers.values())


def test_single_document_default_is_unchanged_and_is_the_primary_document(tmp_path: Path) -> None:
    """Regression: an unmodified single-document configuration -- today's
    only reachable state before this milestone -- keeps exactly one
    analyzer, and the back-compat `document_analyzer` singular property
    (used by the raw PDF viewer endpoints and reconciliation) points at it.
    """
    settings = _settings(tmp_path, ["armie_demo_schedule.pdf"])
    container = ServiceContainer(settings)

    assert list(container.document_analyzers.keys()) == ["armie_demo_schedule.pdf"]
    assert container.document_analyzer is container.document_analyzers["armie_demo_schedule.pdf"]


def test_project_metadata_lists_every_configured_document(tmp_path: Path) -> None:
    settings = _settings(tmp_path, FULL_CORPUS)
    container = ServiceContainer(settings)

    metadata = container.project_metadata()

    assert metadata["pdf_file"] == "armie_demo_schedule.pdf"  # primary, back-compat key
    assert len(metadata["pdf_files"]) == len(FULL_CORPUS) == 18
    assert "schedule_l2_east.pdf" in metadata["pdf_files"]


# --- Failure mode 1: precision failure (cross-document collision, §C/OD-32) -------

def test_a_field_answerable_from_two_documents_is_refused_not_guessed(tmp_path: Path) -> None:
    """Panel-A is a real, distinct panel in both schedule_l2_east.pdf and
    schedule_l2_west.pdf (two wings independently reusing the same generic
    label) -- naming only "Panel-A" is genuinely answerable from either.
    """
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    container, service = _service(settings)

    response = service.invoke(project_resources=_demo(container), thread_id="collision", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "clarification_required"
    assert "schedule_l2_east.pdf" in response.answer_markdown
    assert "schedule_l2_west.pdf" in response.answer_markdown
    assert response.execution_metadata.get("model_call_count", 0) == 0


def test_a_field_answerable_from_only_one_of_several_documents_still_answers(tmp_path: Path) -> None:
    """Regression within the multi-document path itself: Panel-C exists
    only in schedule_l2_east.pdf (west has Panel-D instead) -- the naive
    baseline must still answer correctly when there is no genuine
    collision, not become over-cautious now that more than one document
    is configured.
    """
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    container, service = _service(settings)

    response = service.invoke(project_resources=_demo(container), thread_id="unique-match", viewer_context=None, question="What is the connected load for Panel-C?")

    assert response.disposition.value == "answered"
    assert "19.50" in response.answer_markdown
    assert response.citations
    assert response.citations[0].locator["document"] == "schedule_l2_east.pdf"
    assert response.execution_metadata.get("model_call_count", 0) == 0


# --- Failure mode 2: recall failure (vocabulary mismatch, §C/OD-33) ---------------

def test_an_answer_that_exists_only_in_different_vocabulary_is_an_honest_miss(tmp_path: Path) -> None:
    """rfi_log_047.pdf states, in prose, that Panel-E was sized for a
    "connected capacity of 15.75 kW" and explicitly is not yet in any
    issued schedule. No table in this corpus contains "Panel-E" at all.
    A keyword/field-name baseline cannot find this answer no matter how
    many documents it tries -- it must say so honestly, not guess and not
    silently skip the narrative document without recording that it did.
    """
    settings = _settings(tmp_path, [
        f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf", f"{CORPUS}/rfi_log_047.pdf",
    ])
    container, service = _service(settings)

    response = service.invoke(project_resources=_demo(container), thread_id="recall-failure", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert "15.75" not in response.answer_markdown  # never fabricated from the prose it cannot parse
    assert response.execution_metadata.get("model_call_count", 0) == 0


def test_narrative_documents_never_produce_a_false_table_match(tmp_path: Path) -> None:
    """Distractor documents (RFI entries, meeting minutes) are not required
    to satisfy native_lookup's tabular characterization (D-009) -- they
    must degrade to a graceful miss, never a crash and never a bogus
    single-column "table" accidentally parsed from wrapped prose.
    """
    settings = _settings(tmp_path, [
        f"{CORPUS}/rfi_log_047.pdf", f"{CORPUS}/rfi_log_052.pdf", f"{CORPUS}/rfi_log_058.pdf", f"{CORPUS}/rfi_log_061.pdf",
        f"{CORPUS}/meeting_minutes_2026_02_10.pdf", f"{CORPUS}/meeting_minutes_2026_02_24.pdf", f"{CORPUS}/meeting_minutes_2026_03_10.pdf",
    ])
    container, service = _service(settings)

    response = service.invoke(project_resources=_demo(container), thread_id="all-narrative", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "clarification_required"
    assert response.execution_metadata.get("model_call_count", 0) == 0


# --- Full corpus sanity (mechanism-demonstration scale, not enterprise-scale) -----

def test_the_full_corpus_loads_and_answers_correctly_across_all_document_types(tmp_path: Path) -> None:
    settings = _settings(tmp_path, FULL_CORPUS)
    container, service = _service(settings)

    response = service.invoke(project_resources=_demo(container), thread_id="full-corpus", viewer_context=None, question="What is the connected load for Panel-H?")

    assert response.disposition.value == "answered"
    assert "12.00" in response.answer_markdown
    assert response.citations[0].locator["document"] == "schedule_mezzanine.pdf"
    assert response.execution_metadata.get("model_call_count", 0) == 0


@pytest.mark.parametrize("filename", FULL_CORPUS)
def test_every_corpus_file_actually_exists_and_is_readable(filename: str) -> None:
    """A cheap guard against a fixture typo silently turning a "document
    exists but has no answer" test into a "file does not exist" test --
    the two look identical from a bare disposition assertion alone.
    """
    from app.tools.document.analyzer import DocumentAnalyzer

    analyzer = DocumentAnalyzer(pdf_path=ROOT / "demo_data" / filename, evidence_dir=ROOT / "demo_data")
    assert analyzer.available, f"{filename} is missing from demo_data/"
