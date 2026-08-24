"""Deterministic document extraction tests (SPEC-M2P1 §4.7).

Two levels: direct ``DocumentAnalyzer.native_lookup`` calls against the
committed synthetic fixture (no provider, no network) for the extraction
contract itself, and end-to-end ``AgentService.invoke`` calls driven by
``FakeModelProvider`` to prove the *production* dispatch path -- not just
the analyzer in isolation -- makes zero model calls when deterministic
extraction succeeds, and genuinely reaches vision when it does not.

No Ollama, no network, no downloaded model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.schemas.models import DocumentQueryInput
from app.schemas.vision import VisionEvidenceVerification, VisionFieldExtraction
from app.services import ServiceContainer
from app.tools.document.analyzer import DocumentAnalyzer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]

# SPEC-M2P1 §3 B9 ground truth.
GROUND_TRUTH = {
    ("DB-L1-A", "Connected Load"): "18.50",
    ("DB-L1-A", "Diversity Factor"): "0.75",
    ("DB-L2-B", "Connected Load"): "26.00",
    ("DB-L2-B", "Diversity Factor"): "0.65",
    ("Panel-A", "Connected Load"): "44.50",
    ("Panel-A", "Diversity Factor"): "0.70",
}


def _analyzer(tmp_path: Path) -> DocumentAnalyzer:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return DocumentAnalyzer(ROOT / "demo_data" / "armie_demo_schedule.pdf", evidence_dir)


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf",
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    return settings


# --- item 15: all three boards x both fields, deterministically -----------------------

@pytest.mark.parametrize("board,field", list(GROUND_TRUTH))
def test_native_lookup_answers_every_board_and_field_combination(tmp_path: Path, board: str, field: str) -> None:
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field=field, question=f"What is the {field.lower()} for {board}?")
    result = analyzer.native_lookup(query)

    assert result.value == GROUND_TRUTH[(board, field)]
    assert result.extraction_method == "native_text"
    assert result.bbox is not None and len(result.bbox) == 4
    assert result.confidence >= Settings().pdf_confidence_threshold
    assert result.ambiguity is None
    assert result.evidence and result.evidence[0].locator["bbox"] == result.bbox
    assert result.evidence[0].locator["record"] == board


@pytest.mark.parametrize("board,field", list(GROUND_TRUTH))
def test_agent_service_answers_deterministically_with_zero_model_calls(tmp_path: Path, board: str, field: str) -> None:
    """End-to-end proof (not just the analyzer): the production dispatch
    path in ``_execute_pdf`` never reaches a provider for these six
    combinations. ``FakeModelProvider`` has no scripted response for any
    purpose, so any accidental model call raises ``AssertionError`` from
    the fake itself rather than silently succeeding.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(thread_id=f"det-{board}-{field}", viewer_context=None, question=f"What is the {field.lower()} for {board}?")

    assert response.disposition.value == "answered"
    assert GROUND_TRUTH[(board, field)] in response.answer_markdown
    assert fake.calls == []
    assert response.citations
    assert response.citations[0].locator["bbox"] is not None
    assert response.verification.status == "passed"


# --- item 16: Panel-A regressions (B5 -- target_board's regex never widened) -----------

@pytest.mark.parametrize("field", ["Connected Load", "Diversity Factor"])
def test_panel_a_is_answered_deterministically_without_the_ambiguity_message_or_a_vision_call(tmp_path: Path, field: str) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(thread_id=f"panel-a-{field}", viewer_context=None, question=f"What is the {field.lower()} for Panel-A?")

    assert response.disposition.value == "answered"
    assert GROUND_TRUTH[("Panel-A", field)] in response.answer_markdown
    assert "specify" not in response.answer_markdown.lower()
    assert fake.calls == []


def test_target_board_regex_still_does_not_match_panel_a() -> None:
    """B5, unchanged (OD-9: the regex is not widened -- record identification
    is document-derived instead, proven by the tests above)."""
    assert DocumentAnalyzer.target_board("What is the connected load for Panel-A?") is None


# --- item 17: ambiguity is driven by observed document content, never fabricated ------

def test_no_record_named_is_ambiguous_and_lists_every_candidate_from_the_document(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="Connected Load", question="What is the total connected load?")
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.4
    assert result.ambiguity is not None
    for board in ("DB-L1-A", "DB-L2-B", "Panel-A"):
        assert board in result.ambiguity


def test_multiple_records_named_is_ambiguous_and_lists_the_matched_candidates(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="Connected Load", question="What is the connected load for DB-L1-A and DB-L2-B?")
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.4
    assert "DB-L1-A" in result.ambiguity
    assert "DB-L2-B" in result.ambiguity
    assert "Panel-A" not in result.ambiguity


def test_field_absent_from_the_document_is_a_miss_never_a_fabricated_value(tmp_path: Path) -> None:
    """SPEC-M2P1 §3 B9: no after-diversity column exists in the fixture."""
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="After Diversity Load", question="What is the after diversity load for DB-L1-A?")
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.0
    assert "Board" in result.ambiguity and "Diversity Factor" in result.ambiguity


def test_model_generated_requested_field_that_does_not_match_any_header_is_a_miss(tmp_path: Path) -> None:
    """SPEC-M2P1 §4.3 point 7: an untrusted semantic-planner value is
    validated against real headers, never guessed against."""
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="the overall electrical situation of the building", question="Tell me about the electrical situation.")
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.0


# --- item 18: vision fallback is genuinely reached and still works --------------------

def test_deterministic_miss_falls_back_to_vision_which_still_answers(tmp_path: Path) -> None:
    """The positive counterpart to F12: when deterministic extraction misses
    and the vision fallback *succeeds*, its answer is used -- proving the
    fallback is not just reachable but still functional end-to-end."""
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("pdf_extract", VisionFieldExtraction(
        value="0.70", unit=None, page=1, bbox=[503.0, 287.17, 524.4, 302.29], confidence=0.9,
        rationale="Clearly visible on the drawing.", ambiguity=None,
    ))
    fake.script("pdf_verify", VisionEvidenceVerification(supported=True, confidence=0.9, rationale="Visibly matches the drawing."))
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(
        thread_id="vision-fallback-success", viewer_context=None,
        question="What is the after diversity load for Panel-A in this drawing?",
    )

    assert response.disposition.value == "answered"
    assert response.answer_markdown == "0.70"
    assert [call.purpose for call in fake.calls] == ["pdf_extract", "pdf_verify"]
