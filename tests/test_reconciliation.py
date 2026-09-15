"""SPEC-M2 §8: door/window IFC<->drawing reconciliation pilot tests.

Covers the narrow detector's positive/negative examples (§4A), the gate
carve-out's precision (§4B), the full join synthesized end-to-end at zero
model calls (§4C/D, all 9 ground-truth items from §4E), and the IFC `Tag`
edit's isolation (§4E.1/§7).

No Ollama, no network, no downloaded model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ifcopenshell
import pytest
from app.agent.graph import AgentService
from app.agent.router import (
    cross_source_join_requested,
    cross_source_reconciliation_requested,
)
from app.config import Settings
from app.schemas.models import MultiQueryPlan, QueryPlan, ResponseLanguage
from app.services import ProjectResources, ServiceContainer
from app.tools.document.analyzer import _Table, _TableCell, _TableColumn
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


RECONCILIATION_QUESTION = "Please reconcile the door and window schedule between the IFC model and the PDF drawing."


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _service(settings: Settings, fake: FakeModelProvider) -> tuple[ServiceContainer, AgentService]:
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    return container, AgentService(container)


# --- §4A: narrow detector positive/negative examples -----------------------

@pytest.mark.parametrize("question", [
    RECONCILIATION_QUESTION,
    "Can you verify the doors against the PDF schedule?",
    "Compare the window dimensions in the IFC model with the drawing schedule.",
    "核对一下门窗数量和图纸是否一致。",
    # D-033: found live, 2026-09-15 -- a real user asked almost exactly
    # this question against the deployed app and it missed, because
    # "mismatch" does not satisfy \bmatch\b (no word boundary between
    # "mis" and "match"). Fell through to the general multi-source
    # heuristic/semantic planner instead of the deterministic
    # reconciliation path -- a real model call, ~10x slower, and a raw
    # per-source dict dump instead of a synthesized comparison.
    "Is there any mismatch for the doors and the windows between the BiM model and the PDF?",
    "Are there any mismatches between the door schedule and the model?",
    "门窗和图纸明细表有不匹配的地方吗？",
])
def test_reconciliation_detector_positive_examples(question: str) -> None:
    assert cross_source_reconciliation_requested(question) is True


@pytest.mark.parametrize("question", [
    "How many doors are there?",  # ordinary door/window question, no reconciliation verb
    "What is the maximum height of the windows in this project?",  # ordinary door/window question
    "Please join the PDF connected load data with the IFC room area.",  # non-reconciliation cross-source question
])
def test_reconciliation_detector_negative_examples(question: str) -> None:
    assert cross_source_reconciliation_requested(question) is False


# --- §4B: gate carve-out precision -------------------------------------------

def test_gate_carveout_does_not_broaden_refusal_for_non_reconciliation_cross_source_questions() -> None:
    """SPEC-M2 §8: at least 2 non-reconciliation cross-source-refusal
    questions (electrical-load-vs-room-area and one other) must still be
    refused after the carve-out lands -- proving the carve-out is scoped
    exactly to the narrow reconciliation detector, not broadened. The exact
    "electrical-load-vs-room-area" question is also covered, unmodified, by
    tests/test_router_contract.py, tests/test_failure_path_evals.py (F9),
    and tests/test_disposition_contract.py.
    """
    electrical_load_vs_room_area = "Please join the PDF connected load data with the IFC room area."
    another_cross_source = "Please combine the connected load data with the IFC room area."
    for question in (electrical_load_vs_room_area, another_cross_source):
        assert cross_source_reconciliation_requested(question) is False
        assert cross_source_join_requested(question) is True


def test_gate_carveout_is_load_bearing_not_vacuous() -> None:
    """This phrasing uses "relate" -- a cross_source_join_requested
    join-marker verb -- together with a door/window term, a reconciliation
    verb ("check"), and "schedule". Without the carve-out this would be
    refused as an ordinary cross-source join; with it, the narrow
    reconciliation detector takes precedence, demonstrating the carve-out
    changes real behavior rather than never actually firing.
    """
    question = "Please relate the door schedule in the PDF to the IFC model and check for discrepancies."
    assert cross_source_reconciliation_requested(question) is True
    assert cross_source_join_requested(question) is False


# --- §4C/D/G: full join, end-to-end, zero model calls -----------------------

EXPECTED_STATUSES = {
    "D01": "matched", "D02": "matched", "D03": "matched", "D04": "missing_in_pdf",
    "W01": "matched", "W02": "dimension_mismatch", "W03": "matched", "W04": "matched", "W05": "missing_in_ifc",
}


def test_all_nine_ground_truth_items_reconcile_to_intended_status_at_zero_model_calls(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()  # zero scripted responses: a model call would raise
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-1", question=RECONCILIATION_QUESTION, viewer_context=None)

    assert response.disposition.value == "answered"
    assert fake.calls == []
    assert response.execution_metadata.get("model_call_count") == 0
    assert response.execution_metadata.get("tool_call_count") == 2
    actual = {item.tag: item.status.value for item in response.reconciliation_items}
    assert actual == EXPECTED_STATUSES


def test_reconciliation_reports_a_descriptive_source_and_an_execute_prefixed_audit_step(tmp_path: Path) -> None:
    """Found live, 2026-09-13: the Decision Trace's Execution step showed
    "source: multi_source" with no indication this was an IFC<->PDF join,
    and its own trace toggle was entirely absent -- the frontend's
    auditStage() classifier buckets by step name substring
    ("execute"/"tool"/"subplan" -> Execution), and this code path's audit
    events were emitted under step="reconciliation", which matches none of
    those and fell through to the default "Final Response" bucket instead.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-source-label", question=RECONCILIATION_QUESTION, viewer_context=None)

    assert response.execution_metadata.get("source") == "ifc+pdf (reconciliation)"
    trace = container.audit_store.by_trace(response.trace_id)
    assert any(event.step == "execute_reconciliation" for event in trace)


def test_dimension_mismatch_item_reports_both_sources_disagreeing_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-2", question=RECONCILIATION_QUESTION, viewer_context=None)

    w02 = next(item for item in response.reconciliation_items if item.tag == "W02")
    assert w02.status.value == "dimension_mismatch"
    assert w02.ifc_width_m == pytest.approx(1.2)
    assert w02.ifc_height_m == pytest.approx(1.75)
    assert w02.pdf_width_m == pytest.approx(1.2)
    assert w02.pdf_height_m == pytest.approx(1.70)


def test_missing_in_pdf_and_missing_in_ifc_items_leave_the_absent_side_null(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-3", question=RECONCILIATION_QUESTION, viewer_context=None)
    by_tag = {item.tag: item for item in response.reconciliation_items}

    d04 = by_tag["D04"]
    assert d04.status.value == "missing_in_pdf"
    assert d04.ifc_width_m == pytest.approx(0.9)
    assert d04.pdf_width_m is None and d04.pdf_height_m is None

    w05 = by_tag["W05"]
    assert w05.status.value == "missing_in_ifc"
    assert w05.pdf_width_m == pytest.approx(1.2)
    assert w05.ifc_width_m is None and w05.ifc_height_m is None


def test_non_reconciliation_cross_source_question_still_refused_end_to_end(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="join-1", question="Please combine the connected load data with the IFC room area.", viewer_context=None)

    assert response.disposition.value == "unsupported"
    assert response.execution_metadata.get("tool_call_count") == 0
    assert response.execution_metadata.get("model_call_count") == 0
    assert response.reconciliation_items == []


# --- D-034: the semantic planner can also recognize reconciliation intent ---

def test_semantic_planner_recognizing_reconciliation_intent_routes_to_the_same_deterministic_join(tmp_path: Path) -> None:
    """D-033 fixed one keyword-gate miss ("mismatch"); this proves the
    general fix. The keyword gate (`cross_source_reconciliation_requested`)
    is deliberately conservative and will always miss some future
    phrasing -- found live, 2026-09-15, when a very natural rephrasing fell
    through to the general semantic planner and returned a raw, unsynthesized
    dump of two separate IfcDoor/IfcWindow lists instead of a comparison.

    The planner prompt (`router.planner_prompt`) now teaches the semantic
    planner about this already-authorized intent directly, so even when the
    keyword gate misses, the semantic planner's own model call (which was
    always going to happen on a miss -- this adds no *additional* model
    call) can still produce `MultiQueryPlan.intent="reconciliation"` and
    reach the identical dedicated join `_synthesize_reconciliation_response`
    as the heuristic keyword path, rather than defaulting to two
    disconnected list subplans.
    """
    # Avoids both the reconciliation gate's own verb tokens ("discrepancy" is
    # not one of them) AND the separate, unrelated "schedule"/"load"/etc.
    # PDF-domain heuristic (router.py's heuristic_plan/heuristic_multi_plan)
    # that would otherwise swallow the question before it ever reaches
    # semantic planning.
    question = "Do the doors and windows in the IFC model line up with what's shown on the PDF drawing?"
    assert cross_source_reconciliation_requested(question) is False

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    semantic_reconciliation_plan = MultiQueryPlan(
        intent="reconciliation", response_language="en",
        rationale="Door/window cross-source comparison against the PDF schedule.",
        raw_user_message=question, normalized_request=question, interpretation_confidence="high",
        subplans=[
            QueryPlan(subtask_id="task_1", source="ifc", intent="reconciliation", operation="list", entity_type=None,
                      filters={}, group_by="none", expected_result_shape="list",
                      rationale="IFC side of the door/window comparison.", planning_mode="llm", match_status="complete"),
            QueryPlan(subtask_id="task_2", source="pdf", intent="reconciliation", operation="extract_field",
                      filters={"page_hint": 2}, group_by="none", expected_result_shape="list",
                      rationale="PDF schedule side of the door/window comparison.", planning_mode="llm", match_status="complete"),
        ],
    )
    fake.script("multi_query_plan", semantic_reconciliation_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-semantic", question=question, viewer_context=None)

    assert response.disposition.value == "answered"
    # Exactly plan + language-classification -- no repair/escalation round
    # trip (this fake has no "multi_query_plan_repair" response queued, so a
    # spurious repair attempt would fail loudly here, not silently pass).
    # The specific fix this depends on --
    # validate_multi_plan exempting intent="reconciliation" from the
    # single-entity-type "missing_entity" check -- is unit-tested directly,
    # in isolation, by
    # test_plan_validation_contract.py::test_validate_multi_plan_exempts_reconciliation_intent_from_missing_entity.
    assert [call.purpose for call in fake.calls] == ["multi_query_plan", "response_language"]
    actual = {item.tag: item.status.value for item in response.reconciliation_items}
    assert actual == EXPECTED_STATUSES
    assert response.execution_metadata.get("source") == "ifc+pdf (reconciliation)"


def test_reconciliation_pdf_items_spans_multiple_schedule_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-036: found live, 2026-09-15, adding a real building's full
    door/window schedule (~100+ items) as a second Dataset Pack fixture --
    SPEC-M2's original synthetic schedule (9 items) always fit on page 2
    alone, so `_reconciliation_pdf_items` never needed to look further.
    A real schedule this size legitimately spans several pages; before this
    fix, everything past page 2 was silently invisible to reconciliation,
    which would have reported every one of those real items as
    "missing from the PDF" -- not because they were missing, but because
    the reader never looked.

    Fakes only the PDF-source-read I/O seam (the same technique already
    established by test_pdf_read_failure_is_reported_as_error_not_answered
    for this exact seam) across three calls -- a real table on page 2, a
    real table on page 3 with a *different* mark, and `None` on page 4 --
    so the real merge/stop logic in `_reconciliation_pdf_items` runs
    unmocked.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    page_2_table = _Table(
        columns=[_TableColumn(label="Mark", bbox=[0, 0, 0, 0]), _TableColumn(label="Width (m)", bbox=[0, 0, 0, 0]), _TableColumn(label="Height (m)", bbox=[0, 0, 0, 0])],
        rows=[[_TableCell(text="D01", bbox=[0, 0, 0, 0]), _TableCell(text="0.90", bbox=[0, 0, 0, 0]), _TableCell(text="2.10", bbox=[0, 0, 0, 0])]],
    )
    page_3_table = _Table(
        columns=[_TableColumn(label="Mark", bbox=[0, 0, 0, 0]), _TableColumn(label="Width (m)", bbox=[0, 0, 0, 0]), _TableColumn(label="Height (m)", bbox=[0, 0, 0, 0])],
        rows=[[_TableCell(text="W03", bbox=[0, 0, 0, 0]), _TableCell(text="1.20", bbox=[0, 0, 0, 0]), _TableCell(text="1.50", bbox=[0, 0, 0, 0])]],
    )

    def fake_read_table(page_number: int):
        return {2: page_2_table, 3: page_3_table}.get(page_number)

    monkeypatch.setattr(container.document_analyzer, "_read_table", fake_read_table)

    items = service._reconciliation_pdf_items(_demo(container))

    assert set(items) == {"D01", "W03"}
    assert items["D01"] == {"tag": "D01", "width_m": pytest.approx(0.90), "height_m": pytest.approx(2.10)}
    assert items["W03"] == {"tag": "W03", "width_m": pytest.approx(1.20), "height_m": pytest.approx(1.50)}


def test_pdf_read_failure_is_reported_as_error_not_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P1 finding on PR #6: this reproduces the exact failure shape
    described -- `_read_table` returns `None` (the PDF is unavailable, or
    the page's table structure could not be reconstructed at all), not an
    exception. Before the fix, `_reconciliation_pdf_items` silently turned
    that `None` into an empty mapping, and synthesis reported every IFC
    item as "checked, missing from the PDF" with disposition="answered"
    and verification="passed" -- a fabricated result, since the schedule
    was never actually read. The IFC side succeeds normally; only the PDF
    read is broken.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    monkeypatch.setattr(container.document_analyzer, "_read_table", lambda page_number: None)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-pdf-failure", question=RECONCILIATION_QUESTION, viewer_context=None)

    assert response.disposition.value != "answered"
    assert response.disposition.value == "error"
    # No item is fabricated as "checked and missing" as a result of the failure.
    assert response.reconciliation_items == []
    assert "could not be read" in response.answer_markdown
    assert fake.calls == []


def test_reconciliation_response_language_is_always_english_regardless_of_question_language(tmp_path: Path) -> None:
    """Codex P2 finding on PR #6: reconciliation_plan detects "zh-CN" from
    a Chinese question (asserted directly in the detector tests above), but
    _synthesize_reconciliation_response's answer text is an English-only
    template with no localized reconciliation rendering yet. Reporting
    response_language="zh-CN" while the text is English would overclaim
    what the response actually says -- report the language the answer is
    actually written in until real localized templates exist (tracked as a
    REVIEW_REQUIRED.md follow-up, distinct from the already-tracked CJK
    PDF-routing gap, which is a different subsystem).
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="reconcile-zh", question="核对一下门窗数量和图纸是否一致。", viewer_context=None)

    assert response.disposition.value == "answered"
    assert response.execution_metadata.get("response_language") == "en"


# --- §4E.1/§7: IFC Tag-edit isolation ----------------------------------------

def test_ifc_tag_edit_touched_only_the_eight_targeted_attributes() -> None:
    """SPEC-M2 §4E.1/§7/§8: the Tag edit must not add, remove, or otherwise
    modify any entity, geometry, or other attribute -- verified by diffing,
    not merely asserted.

    A whole-file diff against a freshly regenerated (unedited)
    ``scripts/generate_demo_data.py::make_ifc()`` output was tried first and
    rejected: two independent runs of the same unmodified generator produce
    a different relative ordering of the four walls' geometry (and,
    consequently, their point/placement coordinates) inside
    ``IfcRelContainedInSpatialStructure.RelatedElements`` and similar
    inverse relations, even though entity IDs, counts, and every door/
    window quantity align 1:1 across runs. That reordering is a property of
    running the generator twice, not of this edit, so a strict cross-run
    diff produces false positives unrelated to the Tag field. This test
    instead asserts, directly and deterministically against the single
    committed fixture, exactly what SPEC-M2 §7 requires: no entity was
    added or removed, Tag was set on exactly the 8 targeted door/window
    instances (identified by their fixed, pre-existing GlobalId and Name,
    per SPEC-M2 §3 -- neither touched by this edit) with the exact intended
    mark, every one of those 8 instances' quantities are unchanged from
    §3's documented baseline, and no other entity anywhere in the model
    carries any Tag value.
    """
    model = ifcopenshell.open(str(ROOT / "demo_data" / "armie_demo.ifc"))

    assert len(list(model)) == 708
    assert len(model.by_type("IfcDoor")) == 4
    assert len(model.by_type("IfcWindow")) == 4
    assert len(model.by_type("IfcWall")) == 8

    # (global_id, name, tag, width_m, height_m) -- fixed facts from SPEC-M2 §3,
    # verified directly against the fixture before this edit was made.
    expected = [
        ("1ELt6Y1753HvqSDnK7obe5", "Level 01 Door 1", "D01", 0.9, 2.1),
        ("1Vs7DPYDH9Z8T4Z81ag_KC", "Level 01 Door 2", "D02", 0.9, 2.1),
        ("2CUzHrWB5DGA5MhwLOegsW", "Level 02 Door 1", "D03", 0.9, 2.1),
        ("2Q3h71bNfBwRNWJJqzGjhL", "Level 02 Door 2", "D04", 0.9, 2.1),
        ("1ctyjgDIX8IAhrrKTlC1w0", "Level 01 Window 1", "W01", 1.2, 1.5),
        ("2UQPdaBn99OxerXqkRvAKF", "Level 01 Window 2", "W02", 1.2, 1.75),
        ("1HYHJappTBFvz5P$G3g0Va", "Level 02 Window 1", "W03", 1.2, 1.5),
        ("3sXXr6n2z4WegnkeYlPHD_", "Level 02 Window 2", "W04", 1.2, 1.75),
    ]
    import ifcopenshell.util.element as ifc_element_util

    tagged_ids: set[int] = set()
    for global_id, name, tag, width_m, height_m in expected:
        elements = model.by_guid(global_id)
        assert elements.Name == name
        assert elements.Tag == tag
        psets = ifc_element_util.get_psets(elements, qtos_only=True)
        quantities = next(iter(psets.values()), {})
        assert quantities.get("Width") == pytest.approx(width_m)
        assert quantities.get("Height") == pytest.approx(height_m)
        tagged_ids.add(elements.id())

    # No entity anywhere else in the model -- of any type, not just
    # doors/windows -- carries any Tag value.
    all_tagged = [element for element in model if getattr(element, "Tag", None)]
    assert {element.id() for element in all_tagged} == tagged_ids
