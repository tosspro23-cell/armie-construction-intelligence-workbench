"""Deterministic contract tests for app.agent.router (SPEC-M1 §4.4/13).

Pure functions only; no provider, no network, no fixtures beyond plain dicts.
"""

from __future__ import annotations

import pytest
from app.agent.router import (
    ELEMENT_ALIASES,
    capability_gate,
    cross_source_join_requested,
    fast_path_coverage,
    ground_plan_to_selection,
    heuristic_multi_plan,
    heuristic_plan,
    nearest_space_requested,
    selected_element_plan,
)

# --- fast_path_coverage -----------------------------------------------------

@pytest.mark.parametrize("entity_word", ["doors", "windows", "walls", "spaces", "stairs", "slabs"])
def test_fast_path_coverage_complete_for_each_supported_entity(entity_word: str) -> None:
    coverage = fast_path_coverage(f"how many {entity_word} are there in the project?", {}, has_viewer_context=False)
    assert coverage["coverage_status"] == "complete"
    assert coverage["covered_intents"] == [ELEMENT_ALIASES[entity_word]]
    assert coverage["unresolved_intents"] == []


def test_fast_path_coverage_incomplete_for_non_ascii_question() -> None:
    coverage = fast_path_coverage("这个项目里有多少扇门？", {}, has_viewer_context=False)
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
