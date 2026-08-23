"""Deterministic contract tests for app.agent.plan_validation (SPEC-M1 §4.4/14).

Pure functions only; no provider, no network. Every correction path asserts
both the corrected plan and the emitted correction record.
"""

from __future__ import annotations

from app.agent.plan_validation import (
    actual_result_shape,
    calibrate_interpretation_confidence,
    canonicalize_multi_plan,
    canonicalize_subplan,
    eligible_scalar_count_batch,
    enforce_grouped_request_contract,
    validate_multi_plan,
    verify_execution_consistency,
)
from app.schemas.models import MultiQueryPlan, QueryPlan


def _plan(**overrides) -> QueryPlan:
    defaults = dict(
        subtask_id="task_1", source="ifc", intent="count", operation="count",
        entity_type="IfcDoor", filters={}, group_by="none", rationale="test plan",
        planning_mode="llm", match_status="complete",
    )
    defaults.update(overrides)
    return QueryPlan(**defaults)


def _multi(subplans, **overrides) -> MultiQueryPlan:
    defaults = dict(response_language="en", subplans=subplans, rationale="test")
    defaults.update(overrides)
    return MultiQueryPlan(**defaults)


# --- canonicalize_subplan ------------------------------------------------------

def test_canonicalize_subplan_legacy_max_window_height() -> None:
    plan = _plan(operation="max_window_height", entity_type=None, intent="aggregate")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.operation == "aggregate_quantity"
    assert canonical.entity_type == "IfcWindow"
    assert canonical.measure == "height"
    assert canonical.aggregation == "max"
    assert canonical.expected_result_shape == "scalar_measurement"
    assert len(corrections) == 1
    assert corrections[0]["correction"] == "canonicalized legacy window extremum to aggregate_quantity"


def test_canonicalize_subplan_clears_stale_grouping_on_quantity_request() -> None:
    plan = _plan(operation="aggregate_quantity", entity_type="IfcDoor", measure="Height", aggregation="max", group_by="storey", postprocess="argmax", intent="aggregate")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.group_by == "none"
    assert canonical.postprocess is None
    assert canonical.measure == "height"
    assert any(item["correction"] == "cleared stale grouping/postprocess from latest quantity request" for item in corrections)


def test_canonicalize_subplan_honors_model_declared_unsupported_boundary() -> None:
    plan = _plan(source="ifc", intent="count", rationale="This request is outside the scope of the supported IFC ontology.")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.source == "unsupported"
    assert canonical.intent == "unsupported"
    assert canonical.entity_type is None
    assert any(item["correction"] == "preserved the model-declared unsupported capability boundary" for item in corrections)


def test_canonicalize_subplan_recovers_omitted_fields_from_rationale() -> None:
    plan = _plan(entity_type=None, operation=None, intent="count", rationale="The operation is count for IfcWindow as requested.")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.entity_type == "IfcWindow"
    assert canonical.operation == "count"
    assert any(item["correction"] == "recovered omitted typed fields from the model rationale" for item in corrections)


def test_canonicalize_subplan_recovers_ifc_source_from_typed_fields() -> None:
    plan = _plan(source="unsupported", intent="unsupported", entity_type="IfcDoor", operation="count")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.source == "ifc"
    assert canonical.intent == "count"
    assert any(item["correction"] == "recovered IFC source and intent from explicit typed entity and operation" for item in corrections)


def test_canonicalize_subplan_moves_legacy_filters_group_by_to_canonical_field() -> None:
    plan = _plan(operation="count", group_by="none", filters={"group_by": "storey"})
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.group_by == "storey"
    assert canonical.operation == "group_by"
    assert "group_by" not in canonical.filters
    assert any(item["correction"] == "moved filters.group_by to canonical group_by field" for item in corrections)


def test_canonicalize_subplan_no_op_returns_no_corrections() -> None:
    plan = _plan(expected_result_shape="scalar_count")
    canonical, corrections = canonicalize_subplan(plan)
    assert canonical.model_dump() == plan.model_dump()
    assert corrections == []


# --- canonicalize_multi_plan ----------------------------------------------------

def test_canonicalize_multi_plan_applies_subplan_corrections_and_reports_subtask_id() -> None:
    plan = _plan(operation="max_window_height", entity_type=None, intent="aggregate", subtask_id="task_1")
    multi_plan = _multi([plan])
    canonical, events = canonicalize_multi_plan(multi_plan)
    assert canonical.subplans[0].operation == "aggregate_quantity"
    assert events
    assert events[0]["subtask_id"] == "task_1"


# --- calibrate_interpretation_confidence -----------------------------------------

def test_calibrate_interpretation_confidence_resolves_contradiction_on_complete_plan() -> None:
    plan = _plan(operation="count", group_by="none", postprocess=None, expected_result_shape="scalar_count")
    multi_plan = _multi([plan], requires_clarification=True, normalized_request="count doors", corrections=[{"correction": "x"}], interpretation_confidence="low")
    calibrated, events = calibrate_interpretation_confidence(multi_plan)
    assert calibrated.requires_clarification is False
    assert calibrated.interpretation_confidence == "medium"
    assert len(events) == 1
    assert events[0]["before"]["requires_clarification"] is True
    assert events[0]["after"]["requires_clarification"] is False


def test_calibrate_interpretation_confidence_leaves_incomplete_plan_untouched() -> None:
    plan = _plan(operation="group_by", group_by="storey", expected_result_shape="grouped_counts")
    multi_plan = _multi([plan], requires_clarification=True, normalized_request="count doors by storey", corrections=[{"correction": "x"}], interpretation_confidence="low")
    calibrated, events = calibrate_interpretation_confidence(multi_plan)
    assert calibrated is multi_plan
    assert events == []


# --- enforce_grouped_request_contract ---------------------------------------------

def test_enforce_grouped_request_contract_repairs_lost_grouping() -> None:
    plan = _plan(operation="count", group_by="none", expected_result_shape="scalar_count")
    multi_plan = _multi([plan])
    corrected, events = enforce_grouped_request_contract(multi_plan, "How many doors are there on each floor?")
    assert corrected.subplans[0].operation == "group_by"
    assert corrected.subplans[0].group_by == "storey"
    assert corrected.subplans[0].expected_result_shape == "grouped_counts"
    assert len(events) == 1
    assert events[0]["correction"] == "enforced explicit per-storey grouped-query contract"


def test_enforce_grouped_request_contract_sets_argmax_postprocess() -> None:
    plan = _plan(operation="count", group_by="none")
    multi_plan = _multi([plan])
    corrected, events = enforce_grouped_request_contract(multi_plan, "Which floor has the most doors, broken down by floor?")
    assert corrected.subplans[0].postprocess == "argmax"


def test_enforce_grouped_request_contract_no_op_without_grouping_language() -> None:
    plan = _plan()
    multi_plan = _multi([plan])
    corrected, events = enforce_grouped_request_contract(multi_plan, "How many doors are there?")
    assert corrected is multi_plan
    assert events == []


# --- validate_multi_plan -----------------------------------------------------------

def test_validate_multi_plan_flags_execution_modifier_in_filters() -> None:
    plan = _plan(filters={"group_by": "storey"})
    issues = validate_multi_plan(_multi([plan]))
    assert any(issue.code == "execution_modifier_in_filters" for issue in issues)


def test_validate_multi_plan_flags_grouping_operation_mismatch() -> None:
    plan = _plan(group_by="storey", operation="count")
    issues = validate_multi_plan(_multi([plan]))
    assert any(issue.code == "grouping_operation_mismatch" for issue in issues)


def test_validate_multi_plan_flags_comparison_without_grouping() -> None:
    plan = _plan(group_by="none", operation="count", postprocess="argmax")
    issues = validate_multi_plan(_multi([plan]))
    assert any(issue.code == "comparison_requires_grouping" for issue in issues)


def test_validate_multi_plan_flags_missing_entity_and_operation() -> None:
    plan = _plan(entity_type=None, operation=None, intent="count")
    issues = validate_multi_plan(_multi([plan]))
    codes = {issue.code for issue in issues}
    assert "missing_entity" in codes
    assert "missing_operation" in codes


def test_validate_multi_plan_passes_clean_plan() -> None:
    plan = _plan()
    issues = validate_multi_plan(_multi([plan]))
    assert issues == []


# --- eligible_scalar_count_batch -----------------------------------------------------

def test_eligible_scalar_count_batch_accepts_equivalent_scalar_counts() -> None:
    plans = [
        _plan(entity_type="IfcDoor", expected_result_shape="scalar_count"),
        _plan(entity_type="IfcWindow", subtask_id="task_2", expected_result_shape="scalar_count"),
    ]
    eligible, reason = eligible_scalar_count_batch(plans)
    assert eligible is True


def test_eligible_scalar_count_batch_rejects_single_plan() -> None:
    eligible, reason = eligible_scalar_count_batch([_plan()])
    assert eligible is False
    assert "at least two" in reason


def test_eligible_scalar_count_batch_rejects_mismatched_semantics() -> None:
    plans = [_plan(entity_type="IfcDoor"), _plan(entity_type="IfcWindow", subtask_id="task_2", group_by="storey", operation="group_by")]
    eligible, reason = eligible_scalar_count_batch(plans)
    assert eligible is False


def test_eligible_scalar_count_batch_rejects_grouped_plans() -> None:
    plans = [
        _plan(entity_type="IfcDoor", group_by="storey", operation="group_by", expected_result_shape="grouped_counts"),
        _plan(entity_type="IfcWindow", subtask_id="task_2", group_by="storey", operation="group_by", expected_result_shape="grouped_counts"),
    ]
    eligible, reason = eligible_scalar_count_batch(plans)
    assert eligible is False


# --- actual_result_shape --------------------------------------------------------------

def test_actual_result_shape_scalar_measurement() -> None:
    assert actual_result_shape({"value_m": 2.1, "quantity_path": "Qto"}) == "scalar_measurement"


def test_actual_result_shape_grouped_counts() -> None:
    assert actual_result_shape({"Level 01": 2, "Level 02": 2}) == "grouped_counts"


def test_actual_result_shape_scalar_count() -> None:
    assert actual_result_shape(4) == "scalar_count"


def test_actual_result_shape_list() -> None:
    assert actual_result_shape([{"a": 1}]) == "list"


def test_actual_result_shape_unknown() -> None:
    assert actual_result_shape("some string") == "unknown"


# --- verify_execution_consistency -----------------------------------------------------

def test_verify_execution_consistency_passes_matching_scalar_count() -> None:
    plan = _plan(operation="count", expected_result_shape="scalar_count")
    issues = verify_execution_consistency(plan, tool_query={"operation": "count"}, result_value=4, answer="The project contains **4** doors.")
    assert issues == []


def test_verify_execution_consistency_flags_result_shape_mismatch() -> None:
    plan = _plan(operation="count", expected_result_shape="scalar_count")
    issues = verify_execution_consistency(plan, tool_query={"operation": "count"}, result_value={"Level 01": 2}, answer="grouped")
    assert any(issue.code == "result_shape_mismatch" for issue in issues)


def test_verify_execution_consistency_flags_grouping_not_preserved_by_tool() -> None:
    plan = _plan(operation="group_by", group_by="storey", expected_result_shape="grouped_counts")
    issues = verify_execution_consistency(plan, tool_query={"operation": "count", "group_by": "none"}, result_value={"Level 01": 2}, answer="Per-storey counts: Level 01: 2.")
    assert any(issue.code == "execution_does_not_preserve_grouping_requirement" for issue in issues)


def test_verify_execution_consistency_flags_missing_grouped_result() -> None:
    plan = _plan(operation="group_by", group_by="storey", expected_result_shape="grouped_counts")
    issues = verify_execution_consistency(plan, tool_query={"operation": "group_by", "group_by": "storey"}, result_value=4, answer="4")
    assert any(issue.code == "grouped_result_missing" for issue in issues)


def test_verify_execution_consistency_flags_unrendered_argmax_winner() -> None:
    plan = _plan(operation="group_by", group_by="storey", postprocess="argmax", expected_result_shape="single_group_extremum")
    issues = verify_execution_consistency(
        plan, tool_query={"operation": "group_by", "group_by": "storey"},
        result_value={"Level 01": 2, "Level 02": 5}, answer="Level 01 has the most windows.",
    )
    assert any(issue.code == "postprocess_argmax_not_executed" for issue in issues)


def test_verify_execution_consistency_passes_correctly_rendered_argmax_winner() -> None:
    plan = _plan(operation="group_by", group_by="storey", postprocess="argmax", expected_result_shape="single_group_extremum")
    issues = verify_execution_consistency(
        plan, tool_query={"operation": "group_by", "group_by": "storey"},
        result_value={"Level 01": 2, "Level 02": 5}, answer="**Level 02** has the most windows, with **5**.",
    )
    assert issues == []
