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

import asyncio
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.schemas.models import DocumentQueryInput
from app.schemas.vision import VisionEvidenceVerification, VisionFieldExtraction
from app.services import ProjectResources, ServiceContainer
from app.tools.document.analyzer import DocumentAnalyzer
from fakes.fake_provider import FakeModelProvider


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


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


async def _collect_sse_events(streaming_response) -> list[dict]:
    import json
    events = []
    async for chunk in streaming_response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    return events


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
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

    response = service.invoke(project_resources=_demo(container), thread_id=f"det-{board}-{field}", viewer_context=None, question=f"What is the {field.lower()} for {board}?")

    assert response.disposition.value == "answered"
    assert GROUND_TRUTH[(board, field)] in response.answer_markdown
    assert fake.calls == []
    assert response.citations
    assert response.citations[0].locator["bbox"] is not None
    assert response.verification.status == "verified"


# --- item 16: Panel-A regressions (B5 -- target_board's regex never widened) -----------

@pytest.mark.parametrize("field", ["Connected Load", "Diversity Factor"])
def test_panel_a_is_answered_deterministically_without_the_ambiguity_message_or_a_vision_call(tmp_path: Path, field: str) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id=f"panel-a-{field}", viewer_context=None, question=f"What is the {field.lower()} for Panel-A?")

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
    """SPEC-M2P1 §3 B9: no after-diversity column exists in the fixture.
    Pins `page_hint=1` explicitly -- this is testing one page's own miss
    message, not the multi-page search (see the "checked N pages" test
    below for that honest-coverage claim instead).
    """
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="After Diversity Load", question="What is the after diversity load for DB-L1-A?", page_hint=1)
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.0
    assert "Board" in result.ambiguity and "Diversity Factor" in result.ambiguity


def test_field_absent_from_every_page_states_how_many_pages_were_actually_checked(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-19: with no page_hint, a miss
    must honestly claim only "not found on the pages I checked," not
    silently repeat page 1's own specific message as if it spoke for the
    whole document -- "not found on the pages I looked at" is a weaker,
    different claim than "not found," and this fixture's real 2-page
    document (electrical schedule, then door/window schedule) is exactly
    the case where conflating the two would be misleading.
    """
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="After Diversity Load", question="What is the after diversity load for DB-L1-A?")
    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.0
    assert result.miss_reason == "field_not_on_page"
    assert "2 page(s)" in result.ambiguity
    assert "1" in result.ambiguity and "2" in result.ambiguity


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
        project_resources=_demo(container), thread_id="vision-fallback-success", viewer_context=None,
        question="What is the after diversity load for Panel-A in this drawing?",
    )

    assert response.disposition.value == "answered"
    assert response.answer_markdown == "0.70"
    assert [call.purpose for call in fake.calls] == ["pdf_extract", "pdf_verify"]
    assert response.citations[0].locator["localized"] is True
    assert response.citations[0].locator["bbox"] == [503.0, 287.17, 524.4, 302.29]
    # D-024 #5: every citation now carries which document it came from --
    # previously dropped entirely by _citations(), leaving no way to know.
    assert response.citations[0].source_file == "armie_demo_schedule.pdf"


# --- D-024 #3/#4: an unreliable vision bbox is never silently treated as precise ------

def test_vision_extraction_with_no_bbox_is_marked_unlocalized_not_silently_full_page(tmp_path: Path) -> None:
    """Before this fix, `bbox = extraction.bbox or self.page_bbox(...)` made a
    missing/degenerate model-reported bbox indistinguishable from a genuinely
    precise one: the locator's `bbox` silently became the whole page, and the
    frontend drew a location marker (and a "crop") implying real precision it
    never had. Found live, 2026-09-13, from a real citation whose location
    marker didn't track the cited value.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("pdf_extract", VisionFieldExtraction(
        value="51.20", unit=None, page=1, bbox=None, confidence=0.9,
        rationale="Value visible but exact coordinates not confidently determined.", ambiguity=None,
    ))
    fake.script("pdf_verify", VisionEvidenceVerification(supported=True, confidence=0.9, rationale="Visibly matches the drawing."))
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(
        project_resources=_demo(container), thread_id="vision-no-bbox", viewer_context=None,
        question="What is the after diversity load for Panel-A in this drawing?",
    )

    assert response.disposition.value == "answered"
    citation = response.citations[0]
    assert citation.locator["localized"] is False
    assert citation.locator["bbox"] is None
    # A crop image is still provided (the full page, for manual review) --
    # this is about not claiming false precision, not about hiding evidence.
    assert citation.locator["evidence_crop"] is not None


def test_vision_extraction_with_a_near_full_page_bbox_is_also_marked_unlocalized(tmp_path: Path) -> None:
    """A degenerate-but-non-empty bbox (e.g. the model echoing the page
    bounds) previously passed straight through -- Python's `or` only checks
    truthiness, not plausibility, so a non-empty list bypassed the fallback
    entirely regardless of what it actually contained.
    """
    settings = _settings(tmp_path)
    analyzer = _analyzer(tmp_path)
    page_rect = analyzer.page_bbox(1)
    fake = FakeModelProvider()
    fake.script("pdf_extract", VisionFieldExtraction(
        value="51.20", unit=None, page=1, bbox=page_rect, confidence=0.9,
        rationale="Visible on the page.", ambiguity=None,
    ))
    fake.script("pdf_verify", VisionEvidenceVerification(supported=True, confidence=0.9, rationale="Visibly matches the drawing."))
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(
        project_resources=_demo(container), thread_id="vision-full-page-bbox", viewer_context=None,
        question="What is the after diversity load for Panel-A in this drawing?",
    )

    assert response.citations[0].locator["localized"] is False
    assert response.citations[0].locator["bbox"] is None


# --- item 19: V2's free-form extract_pdf_field can reach a later page ------------------
#
# Found live, 2026-09-18, verifying SPEC-M17's chat-embedded finding
# investigation against the real Azure deployment: the model made three
# separate extract_pdf_field attempts (different phrasings) trying to
# confirm finding W02's PDF height, and every one reported "the visible
# page is an electrical schedule" -- even though the door/window schedule
# row for W02 genuinely exists on page 2 of this very fixture (verified
# directly from the PDF: "W02 L01 Window 1.20 1.70"). The cause: V2's
# `extract_pdf_field` tool schema has no page parameter for the model to
# set, and `_execute_pdf` (graph.py) builds `DocumentQueryInput` with no
# `page_hint`, which `native_lookup` used to default straight to page 1
# and never look further -- unlike the deterministic
# `reconcile_doors_windows` tool, whose own PDF subplan hardcodes
# `page_hint: 2` and so never hit this at all. These tests reproduce the
# miss exactly as observed (no page_hint, field genuinely on page 2, not
# page 1) and confirm `native_lookup`'s new multi-page fallback finds it.

def test_native_lookup_with_no_page_hint_finds_a_field_on_a_later_page(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="Height", question="What is the height of W02?")

    result = analyzer.native_lookup(query)

    assert result.value == "1.70"
    assert result.page == 2
    assert result.confidence >= Settings().pdf_confidence_threshold
    assert result.evidence and result.evidence[0].locator["record"] == "W02"


def test_native_lookup_with_an_explicit_page_hint_never_scans_past_it(tmp_path: Path) -> None:
    """The multi-page fallback is for the free-form tool call only -- a
    caller that already named a page (reconciliation's own schedule
    reader) must get exactly that page and nothing else, unchanged."""
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="Height", question="What is the height of W02?", page_hint=1)

    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.miss_reason == "field_not_on_page"
    assert result.page == 1


def test_v2_extract_pdf_field_tool_call_finds_a_field_on_page_two_with_zero_page_hint(tmp_path: Path) -> None:
    """End-to-end proof through the real production dispatch path the live
    bug actually went through (`_chat_v2` -> `invoke_v2` -> `_execute_pdf`),
    not just the analyzer in isolation -- the model's own tool call, exactly
    as V2 would really issue it, carries no page number at all."""
    import app.main as main_module
    from app.schemas.models import ChatRequest
    from fakes.fake_provider import ScriptedAnswer, ScriptedToolCalls

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("extract_pdf_field", {"field": "Height", "question": "What is the height of window W02?"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["The PDF schedule records W02's height as 1.70 m."]))
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(
        request_id="v2-pdf-page-req-1", thread_id="v2-pdf-page-thread-1",
        question="What is the height of window W02?", engine="v2",
    )))
    events = asyncio.run(_collect_sse_events(response))

    final = next(event for event in events if event["type"] == "final")["response"]
    assert final["disposition"] == "answered"
    assert "1.70" in final["answer_markdown"]


# --- item 20: a genuine continuation table (same header, later page) -------------------
#
# Independent-review finding, 2026-09-19: item 19's fix only kept
# searching on a "field_not_on_page" miss (this page's own columns don't
# have the requested field at all) and stopped on "no_matching_record"
# ("the field exists here but no record matches"), reasoning the latter
# always meant "this page is already the right one." A real continuation
# schedule breaks that: the SAME header row (Mark/Width/Height) repeats
# on every page, each page naming only its own few records, so a page
# that simply doesn't contain the requested record produces the exact
# same "no_matching_record" signature as genuine same-page ambiguity --
# and the old code stopped searching right there, one page short of the
# real answer. Reproduced against a real, freshly-synthesized two-page
# PDF (not the existing single-table fixture, which never exercised this
# same-header-on-both-pages shape) with the exact structure the review
# reported: page 1 names only W01, page 2 (identical header) names only
# W02 with the value actually being asked about.

_CONTINUATION_TABLE_X = [48, 250, 400]


def _write_continuation_schedule(path: Path) -> None:
    import fitz

    document = fitz.open()
    for page_rows in ([("W01", "1.20", "1.50")], [("W02", "1.20", "1.70")]):
        page = document.new_page(width=842, height=595)
        page.insert_text((42, 45), "CONTINUATION SCHEDULE", fontsize=16, fontname="helv", color=(0.08, 0.2, 0.35))
        page.insert_text((42, 66), "Synthetic test fixture -- not a real project document", fontsize=9, fontname="helv", color=(0.3, 0.3, 0.3))
        for row_index, row in enumerate([("Mark", "Width (m)", "Height (m)"), *page_rows]):
            top = 120 + row_index * 42
            page.draw_rect(
                fitz.Rect(_CONTINUATION_TABLE_X[0], top - 22, _CONTINUATION_TABLE_X[-1] + 100, top + 14),
                color=(0.4, 0.5, 0.65), fill=(0.92, 0.95, 0.98) if row_index == 0 else (1, 1, 1), width=0.8,
            )
            for col, value in enumerate(row):
                page.insert_text((_CONTINUATION_TABLE_X[col] + 8, top), value, fontsize=10.5 if row_index else 10, fontname="helv", color=(0.05, 0.1, 0.2))
    document.save(path)


def test_native_lookup_finds_a_record_on_a_continuation_page_with_an_identical_header(tmp_path: Path) -> None:
    pdf_path = tmp_path / "continuation_schedule.pdf"
    _write_continuation_schedule(pdf_path)
    analyzer = DocumentAnalyzer(pdf_path, tmp_path / "evidence")
    (tmp_path / "evidence").mkdir(exist_ok=True)
    query = DocumentQueryInput(field="Height (m)", question="What is the height of W02?")

    result = analyzer.native_lookup(query)

    assert result.value == "1.70"
    assert result.page == 2


def test_native_lookup_still_reports_a_genuine_same_page_ambiguity_immediately(tmp_path: Path) -> None:
    """The fix for the continuation-table gap must not blur into never
    stopping at all -- a page naming *more than one* candidate record,
    none matching the question, is a real "which one did you mean" a
    different page cannot resolve, and must still be reported as such
    without scanning further (this is the existing demo schedule fixture,
    already covered by test_no_record_named_is_ambiguous_..., asserted
    again here to pin the continuation-table fix didn't regress it).
    """
    analyzer = _analyzer(tmp_path)
    query = DocumentQueryInput(field="Connected Load", question="What is the total connected load?")

    result = analyzer.native_lookup(query)

    assert result.value is None
    assert result.confidence == 0.4
    assert result.page == 1
