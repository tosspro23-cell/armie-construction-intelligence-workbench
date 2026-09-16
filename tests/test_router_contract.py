"""Deterministic contract tests for app.agent.router (SPEC-M1 §4.4/13).

Pure functions only; no provider, no network, no fixtures beyond plain dicts
-- except the one end-to-end SPEC-M14 case at the bottom, which drives the
real `AgentService.invoke()` path (mirroring `test_failure_path_evals.py`'s
own `_demo`/`_settings`/`_service` harness) to prove the claim that actually
matters: a real Chinese count question against a real project returns
``answered`` with zero model calls, not just that the planning-layer helpers
return the right shape in isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.agent.router import (
    ELEMENT_ALIASES,
    capability_gate,
    cross_source_join_requested,
    cross_source_reconciliation_requested,
    fast_path_coverage,
    ground_plan_to_selection,
    heuristic_multi_plan,
    heuristic_plan,
    nearest_space_requested,
    selected_element_plan,
)
from app.config import Settings
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]

# --- fast_path_coverage -----------------------------------------------------

@pytest.mark.parametrize("entity_word", ["doors", "windows", "walls", "spaces", "stairs", "slabs"])
def test_fast_path_coverage_complete_for_each_supported_entity(entity_word: str) -> None:
    coverage = fast_path_coverage(f"how many {entity_word} are there in the project?", {}, has_viewer_context=False)
    assert coverage["coverage_status"] == "complete"
    assert coverage["covered_intents"] == [ELEMENT_ALIASES[entity_word]]
    assert coverage["unresolved_intents"] == []


# SPEC-M14, OD-49: this test's own asserted behavior is intentionally
# flipped from its pre-M14 form (which asserted "incomplete" for this
# exact question, when fast_path_coverage's own ascii_only gate had no
# Chinese exemption at all) -- see SPEC-M14's own Invariants section for
# why this is a deliberate, spec-approved reversal, not a silent
# regression.
def test_fast_path_coverage_complete_for_simple_chinese_count_question() -> None:
    coverage = fast_path_coverage("这个项目里有多少扇门？", {}, has_viewer_context=False)
    assert coverage["coverage_status"] == "complete"
    assert coverage["covered_intents"] == ["IfcDoor"]
    assert coverage["unresolved_intents"] == []


def test_fast_path_coverage_incomplete_for_non_chinese_non_ascii_question() -> None:
    """SPEC-M14, OD-49 only narrows the ascii_only gate for Chinese text that
    also matches this function's own enumerated count/group-by term lists --
    it is not a general "any non-ASCII script" exemption. A script this
    function has no term list for (Cyrillic here) still correctly reports
    "incomplete", since none of its literal terms can match it either way.
    """
    coverage = fast_path_coverage("Сколько дверей в проекте?", {}, has_viewer_context=False)
    assert coverage["coverage_status"] == "incomplete"
    assert coverage["unresolved_intents"] == ["semantic_decomposition_required"]
    assert coverage["reason"] == "heuristic_coverage_incomplete"


def test_fast_path_coverage_incomplete_for_compound_request() -> None:
    coverage = fast_path_coverage("how many doors and windows are there?", {}, has_viewer_context=False)
    assert coverage["coverage_status"] == "incomplete"


# --- cross_source_join_requested --------------------------------------------

def test_cross_source_join_detected_when_all_three_signals_present() -> None:
    assert cross_source_join_requested("Can you join the PDF connected load with the IFC room area?") is True


@pytest.mark.parametrize("question", [
    "How many doors are there?",
    "What is the connected load for Panel-A?",
    "Combine the numbers for me please.",
])
def test_cross_source_join_not_detected_without_all_three_signals(question: str) -> None:
    assert cross_source_join_requested(question) is False


def test_cross_source_join_is_refused_not_downgraded_in_heuristic_plan() -> None:
    plan = heuristic_plan("Please join the PDF connected load data with the IFC room area.", {}, has_viewer_context=False)
    assert plan.source == "unsupported"
    assert plan.intent == "unsupported"
    assert plan.match_status == "complete"
    supported, reason = capability_gate(plan)
    assert supported is False
    assert reason


def test_heuristic_multi_plan_generic_count_case() -> None:
    multi_plan = heuristic_multi_plan("how many doors and windows are there?", {}, has_viewer_context=False)
    assert multi_plan is not None
    assert multi_plan.intent == "multi_query"
    entity_types = {plan.entity_type for plan in multi_plan.subplans}
    assert entity_types == {"IfcDoor", "IfcWindow"}
    assert all(plan.operation == "count" for plan in multi_plan.subplans)


def test_heuristic_multi_plan_gracefully_refuses_cross_source_join() -> None:
    """SPEC-M1.5 §4C: previously a real defect (see git history) --
    heuristic_multi_plan's own cross-source-join branch constructed
    ``MultiQueryPlan(subplans=[], ...)``, violating the schema's own
    ``min_length=1`` and raising ``ValidationError`` instead of returning the
    intended graceful refusal. Fixed to construct a valid single
    ``source="unsupported"`` subplan, matching heuristic_plan's own
    single-plan equivalent.

    This branch remains unreachable from the live graph: AgentService
    ``_resolve_context`` is the single authoritative cross-source-join gate
    and diverts to "refuse" before "route" (and therefore
    heuristic_multi_plan) is ever invoked (verified end-to-end by F9 in
    test_failure_path_evals.py). heuristic_multi_plan is still directly
    tested here, independent of the graph, because it is directly callable.
    """
    multi_plan = heuristic_multi_plan("Please join the PDF connected load data with the IFC room area.", {}, has_viewer_context=False)
    assert multi_plan is not None
    assert multi_plan.intent == "unsupported"
    assert len(multi_plan.subplans) == 1
    assert multi_plan.subplans[0].source == "unsupported"
    assert multi_plan.subplans[0].intent == "unsupported"


# --- nearest_space_requested -------------------------------------------------

@pytest.mark.parametrize("question", ["Which room is nearest to the lobby?", "Find the closest space to Bedroom 1."])
def test_nearest_space_requested_detected(question: str) -> None:
    assert nearest_space_requested(question) is True


@pytest.mark.parametrize("question", ["How many rooms are there?", "What is the closest door width?"])
def test_nearest_space_requested_not_detected(question: str) -> None:
    assert nearest_space_requested(question) is False


# --- selected_element_plan / deictic follow-up grounding --------------------

def test_deictic_follow_up_is_grounded_to_selected_element() -> None:
    context = {"active_entity_ids": ["2N1SX$3fH1EfXpZL8W3Kk_"], "active_entity_type": "IfcDoor"}
    plan = selected_element_plan("What is this?", context)
    assert plan is not None
    assert plan.source == "ifc"
    assert plan.operation == "get_properties"
    assert plan.entity_type == "IfcDoor"
    assert plan.filters["global_ids"] == context["active_entity_ids"]
    assert plan.match_status == "complete"
    assert plan.planning_mode == "context"


def test_selected_element_plan_none_without_active_selection() -> None:
    assert selected_element_plan("What is this?", {}) is None


def test_selected_element_plan_none_without_deictic_phrasing() -> None:
    context = {"active_entity_ids": ["abc"], "active_entity_type": "IfcDoor"}
    assert selected_element_plan("How many doors are there?", context) is None


def test_selected_element_plan_storey_shape_for_floor_question() -> None:
    context = {"active_entity_ids": ["abc"], "active_entity_type": "IfcWindow"}
    plan = selected_element_plan("Which storey is it on?", context)
    assert plan is not None
    assert plan.expected_result_shape == "element_storey"


# --- heuristic_plan -----------------------------------------------------------

@pytest.mark.parametrize("entity_word,ifc_type", [
    ("door", "IfcDoor"), ("window", "IfcWindow"), ("wall", "IfcWall"),
    ("space", "IfcSpace"), ("stair", "IfcStair"), ("slab", "IfcSlab"),
])
def test_heuristic_plan_recognises_each_supported_entity_alias(entity_word: str, ifc_type: str) -> None:
    plan = heuristic_plan(f"how many {entity_word}s are there?", {}, has_viewer_context=False)
    assert plan.source == "ifc"
    assert plan.entity_type == ifc_type
    assert plan.operation == "count"
    assert plan.match_status == "complete"


# --- SPEC-M14: Chinese count/group-by fast path -----------------------------

@pytest.mark.parametrize("entity_word,ifc_type", [
    ("门", "IfcDoor"), ("窗", "IfcWindow"), ("墙", "IfcWall"), ("空间", "IfcSpace"),
    ("房间", "IfcSpace"), ("楼梯", "IfcStair"), ("楼板", "IfcSlab"), ("屋顶", "IfcRoof"),
    ("柱", "IfcColumn"), ("梁", "IfcBeam"), ("栏杆", "IfcRailing"), ("饰面", "IfcCovering"),
    ("家具", "IfcFurnishingElement"), ("基础", "IfcFooting"),
])
def test_heuristic_plan_recognises_each_chinese_entity_alias(entity_word: str, ifc_type: str) -> None:
    plan = heuristic_plan(f"这个项目里有多少{entity_word}？", {}, has_viewer_context=False)
    assert plan.source == "ifc"
    assert plan.entity_type == ifc_type
    assert plan.operation == "count"
    assert plan.match_status == "complete"
    supported, reason = capability_gate(plan)
    assert supported is True
    assert reason is None


def test_heuristic_plan_chinese_storey_grouping() -> None:
    plan = heuristic_plan("按楼层统计窗户数量", {}, has_viewer_context=False)
    assert plan.entity_type == "IfcWindow"
    assert plan.operation == "group_by"
    assert plan.group_by == "storey"
    assert plan.match_status == "complete"


@pytest.mark.parametrize("question,postprocess", [
    ("哪层的门最多？", "argmax"),
    ("哪层的窗最少？", "argmin"),
])
def test_heuristic_plan_chinese_storey_argmax_argmin(question: str, postprocess: str) -> None:
    plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert plan.group_by == "storey"
    assert plan.postprocess == postprocess
    assert plan.match_status == "complete"


def test_heuristic_multi_plan_chinese_multi_entity_count() -> None:
    multi_plan = heuristic_multi_plan("这个项目里有多少扇门和窗？", {}, has_viewer_context=False)
    assert multi_plan is not None
    entity_types = {plan.entity_type for plan in multi_plan.subplans}
    assert entity_types == {"IfcDoor", "IfcWindow"}
    assert all(plan.operation == "count" for plan in multi_plan.subplans)
    assert multi_plan.response_language == "zh-CN"


def test_heuristic_multi_plan_bare_ambiguous_board_stays_unsupported() -> None:
    """SPEC-M14: bare "板" is deliberately not an alias (ambiguous with a PDF
    electrical panel/board, see AgentService._resolve_context's own
    "这张图里的板有多少" clarification) -- this question must keep falling
    through, unaffected by the Chinese fast path added here.
    """
    assert heuristic_multi_plan("这张图里的板有多少", {}, has_viewer_context=False) is None
    plan = heuristic_plan("这张图里的板有多少", {}, has_viewer_context=False)
    assert plan.source == "unsupported"


@pytest.mark.parametrize("entity_word,ifc_type", [
    ("roof", "IfcRoof"), ("column", "IfcColumn"), ("beam", "IfcBeam"), ("member", "IfcMember"),
    ("railing", "IfcRailing"), ("covering", "IfcCovering"), ("footing", "IfcFooting"), ("plate", "IfcPlate"),
])
def test_heuristic_plan_recognises_each_d054_viewer_entity_alias(entity_word: str, ifc_type: str) -> None:
    """D-055: D-054 made these types real and renderable in the 3D viewer, but
    the query-planning whitelist (ELEMENT_ALIASES/SUPPORTED_ENTITY_TYPES) was
    never updated alongside it, so a natural-language count question for any
    of them fell through to ``unsupported`` even though the viewer could
    already render and highlight the element. Confirmed to genuinely fail
    pre-fix: with this alias absent, heuristic_plan falls back to
    source="unsupported" and capability_gate rejects it.
    """
    plan = heuristic_plan(f"how many {entity_word}s are there?", {}, has_viewer_context=False)
    assert plan.source == "ifc"
    assert plan.entity_type == ifc_type
    assert plan.operation == "count"
    assert plan.match_status == "complete"
    supported, reason = capability_gate(plan)
    assert supported is True
    assert reason is None


def test_heuristic_plan_recognises_furniture_alias() -> None:
    """D-055: "furniture"/"furnishing" alias to IfcFurnishingElement, the noun
    the owner actually used when reporting this gap.
    """
    for word in ("furniture", "furnishings"):
        plan = heuristic_plan(f"how many pieces of {word} are there?", {}, has_viewer_context=False)
        assert plan.source == "ifc"
        assert plan.entity_type == "IfcFurnishingElement"
        assert plan.operation == "count"


def test_ifc_building_element_proxy_deliberately_has_no_query_alias() -> None:
    """D-055: IfcBuildingElementProxy renders in the viewer (D-054) but is a
    generic catch-all with no natural single English word a user would name
    it by, so it is deliberately excluded from ELEMENT_ALIASES -- not an
    oversight.
    """
    assert "IfcBuildingElementProxy" not in ELEMENT_ALIASES.values()


def test_heuristic_plan_pdf_domain_terms_route_to_pdf() -> None:
    plan = heuristic_plan("What is the connected load for Panel-A?", {}, has_viewer_context=False)
    assert plan.source == "pdf"
    assert plan.operation == "extract_field"


def test_heuristic_plan_unsupported_when_nothing_recognised() -> None:
    plan = heuristic_plan("Tell me a joke about scaffolding.", {}, has_viewer_context=False)
    assert plan.source == "unsupported"
    assert plan.match_status == "unknown"


def test_heuristic_plan_source_override_preserves_operation() -> None:
    plan = heuristic_plan("How many windows per storey?", {}, has_viewer_context=False, source_preference="pdf")
    assert plan.source == "pdf"
    assert plan.operation == "extract_field"


def test_heuristic_plan_quantity_extremum_paraphrase() -> None:
    plan = heuristic_plan("Find the tallest door.", {}, has_viewer_context=False)
    assert plan.operation == "aggregate_quantity"
    assert plan.entity_type == "IfcDoor"
    assert plan.measure == "height"
    assert plan.aggregation == "max"


# --- capability_gate ----------------------------------------------------------

def test_capability_gate_rejects_unsupported_source() -> None:
    plan = heuristic_plan("Tell me a joke about scaffolding.", {}, has_viewer_context=False)
    supported, reason = capability_gate(plan)
    assert supported is False
    assert reason


def test_capability_gate_rejects_unsupported_entity_type() -> None:
    plan = heuristic_plan("how many doors are there?", {}, has_viewer_context=False).model_copy(update={"entity_type": "IfcFurniture"})
    supported, reason = capability_gate(plan)
    assert supported is False
    assert "IfcFurniture" in reason


def test_capability_gate_accepts_valid_ifc_count_plan() -> None:
    plan = heuristic_plan("how many doors are there?", {}, has_viewer_context=False)
    supported, reason = capability_gate(plan)
    assert supported is True
    assert reason is None


# --- SPEC-M15: Chinese fast path, phase 2 -----------------------------------

@pytest.mark.parametrize("question,measure,aggregation", [
    ("这个项目最高的门是哪个？", "height", "max"),
    ("门的最大高度是多少？", "height", "max"),
    ("哪个窗户面积最大？", "area", "max"),
    ("最小的墙宽度是多少", "width", "min"),
    ("哪扇门最矮？", "height", "min"),
    ("门的高度最大值是多少", "height", "max"),
])
def test_heuristic_plan_chinese_quantity_extrema_decomposition(question: str, measure: str, aggregation: str) -> None:
    """SPEC-M15 SS A: unlike English's own fixed-word-order `re.fullmatch`
    templates, Chinese phrasing for this intent has no fixed order -- these
    six variants are deliberately different orderings of the same meaning.
    """
    plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert plan.source == "ifc"
    assert plan.operation == "aggregate_quantity"
    assert plan.measure == measure
    assert plan.aggregation == aggregation
    assert plan.match_status == "complete"


def test_heuristic_plan_chinese_quantity_extrema_uses_full_alias_table() -> None:
    """SS A's decomposition deliberately covers the full ELEMENT_ALIASES
    table, not English's own narrower fixed entity set (doors/windows/
    walls/slabs/stairs only) -- verified against a type outside that set.
    """
    plan = heuristic_plan("这根柱子的最大高度是多少？", {}, has_viewer_context=False)
    assert plan.entity_type == "IfcColumn"
    assert plan.measure == "height"
    assert plan.aggregation == "max"


def test_heuristic_plan_chinese_quantity_extrema_partial_without_direction() -> None:
    """An entity and a measure noun alone, with no direction cue, must not
    guess a plan -- falls through exactly like an incomplete English
    phrasing would.
    """
    plan = heuristic_plan("门的高度是多少？", {}, has_viewer_context=False)
    assert plan.operation != "aggregate_quantity"


@pytest.mark.parametrize("question", ["核对一下门的数量和图纸是否匹配", "检查窗户与图纸排程是否一致"])
def test_reconciliation_chinese_phrasing_detected(question: str) -> None:
    assert cross_source_reconciliation_requested(question) is True


def test_reconciliation_chinese_positive_match_verb_gap() -> None:
    """SPEC-M15 SS B: confirmed to genuinely fail pre-fix -- the verb marker
    only ever had the negated "不匹配" (mismatch), never the bare positive
    "匹配" (match), so this exact natural phrasing went undetected.
    """
    assert cross_source_reconciliation_requested("门的数量和图纸是否匹配") is True


@pytest.mark.parametrize("question,from_space,to_space", [
    ("从卧室到厨房的距离是多少？", "卧室", "厨房"),
    ("卧室到厨房的距离是多少", "卧室", "厨房"),
    ("卧室到厨房有多远？", "卧室", "厨房"),
    ("从主卧到客厅有多远", "主卧", "客厅"),
])
def test_heuristic_plan_chinese_space_distance(question: str, from_space: str, to_space: str) -> None:
    plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert plan.operation == "space_distance"
    assert plan.entity_type == "IfcSpace"
    assert plan.filters["from_space"] == from_space
    assert plan.filters["to_space"] == to_space
    assert plan.match_status == "complete"


@pytest.mark.parametrize("question", ["这个视图里能看到什么？", "当前视图是开放还是封闭的？", "现在这个视角能看到什么", "这个视图中门是否可见"])
def test_heuristic_plan_chinese_viewer_snapshot_phrases(question: str) -> None:
    plan = heuristic_plan(question, {}, has_viewer_context=True)
    assert plan.source == "viewer_snapshot"
    assert plan.operation == "inspect_view"
    assert plan.match_status == "complete"


def test_heuristic_plan_chinese_viewer_snapshot_partial_without_viewer_context() -> None:
    plan = heuristic_plan("这个视图里能看到什么？", {}, has_viewer_context=False)
    assert plan.match_status == "partial"


def test_heuristic_plan_chinese_get_properties_keyword() -> None:
    plan = heuristic_plan("门有什么属性？", {}, has_viewer_context=False)
    assert plan.source == "ifc"
    assert plan.intent == "property_lookup"
    assert plan.operation == "get_properties"
    assert plan.match_status == "complete"


def test_heuristic_plan_get_properties_no_longer_crashes_without_context() -> None:
    """SPEC-M15 SS E: real pre-existing bug, found (not introduced) while
    adding the Chinese "属性" keyword -- `intent="get_properties"` is not a
    valid QueryPlan.intent literal. Reproduced independent of Chinese input:
    this exact call raised a pydantic ValidationError before the fix.
    """
    plan = heuristic_plan("what properties does the door have?", {}, has_viewer_context=False)
    assert plan.intent == "property_lookup"
    assert plan.operation == "get_properties"


@pytest.mark.parametrize("question,entity_type,shape", [
    ("有几扇窗？", "IfcWindow", "count"),
    ("总共有多少个空间？", "IfcSpace", "count"),
    ("门扇的数量是多少？", "IfcDoor", "count"),
    ("窗子有多少个？", "IfcWindow", "count"),
    ("有多少个地基？", "IfcFooting", "count"),
    ("各层的门数量", "IfcDoor", "group_by"),
    ("逐层统计窗户", "IfcWindow", "group_by"),
])
def test_heuristic_plan_chinese_surface_broadening(question: str, entity_type: str, shape: str) -> None:
    plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert plan.entity_type == entity_type
    assert plan.operation == shape


@pytest.mark.parametrize("question", [
    "我问的是这个建筑物里的所有窗户有几种类型，每种类型的大小分别是多少？长宽是多少？",
    "这个建筑里有几类窗户",
    "有几种门？",
])
def test_zh_count_intent_excludes_how_many_kinds_not_how_many_items(question: str) -> None:
    """Owner-reported, 2026-09-16: a real regression, live in production.

    SPEC-M15 SS F's bare "有几" marker matched inside "有几种类型" ("how many
    *kinds*"), a fundamentally different, ungrouped-by-attribute request
    this deterministic fast path does not support -- the live app answered
    "所有窗户有几种类型，每种类型的大小...是多少" with a flat total window
    count, silently discarding the actual per-type-dimension question.
    Confirmed to genuinely fail pre-fix: `heuristic_plan` returned a
    `complete` plain `count` plan for the exact production question above.
    Neither `heuristic_plan` nor `heuristic_multi_plan` may now claim
    `complete`/non-`None` for any of these -- they must fall through to the
    semantic planner, which is honest about not fully supporting a
    group-by-distinct-dimension request rather than confidently answering
    the wrong question.
    """
    plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert plan.match_status != "complete"
    assert heuristic_multi_plan(question, {}, has_viewer_context=False) is None


def test_zh_count_intent_still_recognises_genuine_how_many_forms() -> None:
    for question, entity_type in [("有几扇窗？", "IfcWindow"), ("有几个空间", "IfcSpace"), ("有几道门", "IfcDoor")]:
        plan = heuristic_plan(question, {}, has_viewer_context=False)
        assert plan.entity_type == entity_type
        assert plan.operation == "count"
        assert plan.match_status == "complete"
    assert plan.match_status == "complete"


# --- D-060: get_properties on multiple matches must not claim a single ------
# selection

@pytest.mark.parametrize("question,expected_count", [
    ("what properties do the windows have?", 4),
    ("窗户有什么属性？", 4),
])
def test_get_properties_with_multiple_matches_reports_the_real_count_end_to_end(question: str, expected_count: int, tmp_path: Path) -> None:
    """Owner-reported live in production, 2026-09-16: a follow-up asking for
    windows' dimensions in general (not "this" one) was answered
    "当前选中的是一个窗...所在楼层：Level 1" -- a specific, singular claim,
    even though nothing was ever actually selected. Root cause: `armie_demo`
    has 4 real windows; `get_properties` with an entity-type-wide (no
    global_ids) filter matches all 4, but both `_format_ifc_answer` and its
    Chinese counterpart in `_natural_answer` unconditionally took
    `value[0]`/`records[0]` and phrased it as "The selected element
    is..."/"当前选中的是..." -- an arbitrary, misleadingly confident pick
    from whichever element happened to sort first, not a genuine selection.
    Confirmed to genuinely fail pre-fix (`git stash` reran: both messages
    named exactly one "Level 01 Window 1" as if uniquely selected).
    """
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    resources: ProjectResources = asyncio.run(container.get_project("demo"))

    response = service.invoke(project_resources=resources, thread_id=f"d060-{question}", viewer_context=None, question=question)

    assert response.disposition.value == "answered"
    assert response.execution_metadata.get("model_call_count", 0) == 0
    assert str(expected_count) in response.answer_markdown
    assert "当前选中的是一个" not in response.answer_markdown
    assert "The selected element is" not in response.answer_markdown


def test_chinese_quantity_extremum_is_answered_deterministically_end_to_end(tmp_path: Path) -> None:
    """Mirrors SPEC-M14 SS E's own live end-to-end harness: proves the real
    AgentService.invoke() dispatch order, not just the planning-layer shape.
    """
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    resources: ProjectResources = asyncio.run(container.get_project("demo"))

    response = service.invoke(project_resources=resources, thread_id="m15-zh-quantity", viewer_context=None, question="这个项目最高的门是哪个？")

    assert response.disposition.value == "answered"
    assert response.execution_metadata.get("model_call_count", 0) == 0
    assert response.execution_metadata.get("planning_mode") == "heuristic"


# --- ground_plan_to_selection --------------------------------------------------

def test_ground_plan_to_selection_grounds_to_active_selection() -> None:
    context = {"active_entity_ids": ["abc123"], "active_entity_type": "IfcDoor"}
    base_plan = heuristic_plan("how many doors are there?", context, has_viewer_context=False)
    grounded, was_grounded = ground_plan_to_selection(base_plan, context, "What is this?")
    assert was_grounded is True
    assert grounded.filters["global_ids"] == ["abc123"]


def test_ground_plan_to_selection_leaves_plan_unchanged_without_selection() -> None:
    plan = heuristic_plan("how many doors are there?", {}, has_viewer_context=False)
    grounded, was_grounded = ground_plan_to_selection(plan, {}, "how many doors are there?")
    assert was_grounded is False
    assert grounded is plan


# --- SPEC-M14: live end-to-end proof, not just planning-layer shape ---------

def test_chinese_count_question_is_answered_deterministically_end_to_end(tmp_path: Path) -> None:
    """The real claim this spec makes: a genuine Chinese count question,
    driven through the full `AgentService.invoke()` graph against a real
    project, is `answered` with zero model calls -- `heuristic_multi_plan`
    returning the right shape in isolation (the tests above) is necessary
    but not sufficient proof of that; `AgentService._route` has its own
    dispatch order (`generic_multi is not None` checked before the LLM
    branch) that only an end-to-end call actually exercises.
    """
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    resources: ProjectResources = asyncio.run(container.get_project("demo"))

    response = service.invoke(project_resources=resources, thread_id="m14-zh-count", viewer_context=None, question="这个项目里有多少扇门？")

    assert response.disposition.value == "answered"
    assert response.execution_metadata.get("model_call_count", 0) == 0
    assert response.execution_metadata.get("planning_mode") == "heuristic"
