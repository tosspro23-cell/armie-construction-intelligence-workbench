"""Deterministic semantic-plan contracts.

LLMs may produce schema-valid JSON that still loses an execution requirement.
This module keeps canonicalization, cross-field validation, batch eligibility,
and execution/result-shape checks independent from prompt wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.schemas.models import MultiQueryPlan, QueryPlan

EXECUTION_MODIFIERS = {"group_by", "sort_by", "limit", "aggregation", "comparison", "postprocess"}


def enforce_grouped_request_contract(multi_plan: MultiQueryPlan, question: str) -> tuple[MultiQueryPlan, list[dict[str, Any]]]:
    """Repair only an explicit floor/storey grouping request that lost structure.

    This is a narrow semantic safety net at the plan boundary, not a general
    language router.  It prevents a schema-valid local-model plan from silently
    replacing a requested per-storey analysis with a project-wide total.
    """
    normalized = " ".join(question.lower().split())
    grouping_markers = (
        "each floor", "each storey", "per floor", "per storey", "by floor", "by storey",
        "break down", "按楼层", "按层", "按 storey", "每一层", "每层", "哪一层", "哪层", "分别统计", "分别有多少",
    )
    if not any(marker in normalized for marker in grouping_markers):
        return multi_plan, []
    argmax_markers = ("most", "maximum", "greatest number", "highest count", "最多")
    argmin_markers = ("fewest", "least", "minimum", "最少")
    postprocess = "argmax" if any(marker in normalized for marker in argmax_markers) else "argmin" if any(marker in normalized for marker in argmin_markers) else None
    # Preserve the planner's typed entities, but execute each target once.  A
    # duplicate scalar + grouped subplan is not a faithful answer to a pure
    # breakdown request.
    by_entity: dict[str, QueryPlan] = {}
    for plan in multi_plan.subplans:
        if plan.source == "ifc" and plan.entity_type:
            previous = by_entity.get(plan.entity_type)
            if previous is None or plan.group_by in {"storey", "space", "type"}:
                by_entity[plan.entity_type] = plan
    if not by_entity:
        return multi_plan, []
    corrected: list[QueryPlan] = []
    events: list[dict[str, Any]] = []
    for index, plan in enumerate(by_entity.values(), start=1):
        updated = plan.model_copy(update={
            "subtask_id": plan.subtask_id or f"task_{index}",
            "source": "ifc",
            "intent": "aggregate",
            "operation": "group_by",
            "group_by": "storey",
            "postprocess": postprocess,
            "aggregation": None,
            # Keep grouped extrema on the shared grouped-counts contract;
            # postprocess identifies the winning group without changing the
            # evidence-bearing result shape.
            "expected_result_shape": "grouped_counts",
        })
        if updated.model_dump() != plan.model_dump():
            events.append({
                "subtask_id": updated.subtask_id,
                "correction": "enforced explicit per-storey grouped-query contract",
                "before": {"operation": plan.operation, "group_by": plan.group_by, "postprocess": plan.postprocess},
                "after": {"operation": updated.operation, "group_by": updated.group_by, "postprocess": updated.postprocess},
            })
        corrected.append(updated)
    return multi_plan.model_copy(update={
        "intent": "multi_query" if len(corrected) > 1 else "single_query",
        "subplans": corrected,
    }), events


@dataclass(frozen=True)
class PlanIssue:
    subtask_id: str | None
    code: str
    message: str


def canonicalize_subplan(plan: QueryPlan, context: dict[str, Any] | None = None) -> tuple[QueryPlan, list[dict[str, Any]]]:
    """Move one unambiguous legacy grouping field into its canonical home."""
    filters = dict(plan.filters)
    corrections: list[dict[str, Any]] = []
    # Recover omitted structured fields only from the model's own explicit
    # semantic rationale. This is intentionally independent of the raw user
    # language and avoids language-specific fallback routing.
    rationale = plan.rationale or ""
    if plan.operation == "max_window_height":
        plan = plan.model_copy(update={
            "operation": "aggregate_quantity", "intent": "aggregate", "entity_type": plan.entity_type or "IfcWindow",
            "measure": plan.measure or "height", "aggregation": plan.aggregation or "max",
            "expected_result_shape": "scalar_measurement",
        })
        corrections.append({"correction": "canonicalized legacy window extremum to aggregate_quantity", "after": {"operation": "aggregate_quantity", "measure": "height", "aggregation": "max"}})
    if plan.operation == "aggregate_quantity":
        updates: dict[str, Any] = {
            "group_by": "none", "postprocess": None,
            "expected_result_shape": "scalar_measurement",
            "measure": (plan.measure or "").lower() or None,
            "aggregation": plan.aggregation or "max",
        }
        # Quantity extrema are project-wide unless the latest turn explicitly
        # supplies a source-data filter. Never carry a prior grouped winner or
        # postprocessing modifier into this scalar contract.
        if plan.group_by not in (None, "none") or plan.postprocess not in (None, "none"):
            corrections.append({"correction": "cleared stale grouping/postprocess from latest quantity request"})
        plan = plan.model_copy(update=updates)
    # A local semantic planner can occasionally serialize its explicit
    # "outside supported scope" conclusion into an IFC enum fallback.  Honor
    # that model-declared conclusion rather than invoking an unrelated IFC
    # tool and surfacing an internal consistency error to the user.
    rationale_lower = rationale.lower()
    unsupported_markers = ("outside the scope", "outside the supported", "not supported by the provided", "cannot be answered from")
    if plan.source != "unsupported" and any(marker in rationale_lower for marker in unsupported_markers):
        before = {"source": plan.source, "intent": plan.intent, "operation": plan.operation, "entity_type": plan.entity_type}
        plan = plan.model_copy(update={
            "source": "unsupported", "intent": "unsupported", "operation": None,
            "entity_type": None, "filters": {}, "group_by": None, "postprocess": None,
            "expected_result_shape": None,
        })
        corrections.append({
            "correction": "preserved the model-declared unsupported capability boundary",
            "before": before,
            "after": {"source": "unsupported", "intent": "unsupported"},
        })
    recovered_entity = plan.entity_type
    if not recovered_entity:
        match = re.search(r"\b(IfcDoor|IfcWindow|IfcWall|IfcSpace|IfcStair|IfcSlab)\b", rationale)
        recovered_entity = match.group(1) if match else None
    recovered_operation = plan.operation
    if not recovered_operation:
        match = re.search(r"\boperation(?:\s+is|=)\s+(count|list|group_by|get_properties|inspect_relationship)\b", rationale, flags=re.IGNORECASE)
        recovered_operation = match.group(1).lower() if match else None
    if (recovered_entity != plan.entity_type or recovered_operation != plan.operation) and recovered_entity and recovered_operation:
        filters = dict(plan.filters)
        if context and context.get("active_storey") and any(marker in rationale.lower() for marker in ("active storey", "that floor", "that storey", "filter on storey")):
            filters.setdefault("storey", context["active_storey"])
        plan = plan.model_copy(update={"entity_type": recovered_entity, "operation": recovered_operation, "filters": filters})
        corrections.append({
            "correction": "recovered omitted typed fields from the model rationale",
            "before": {"entity_type": None, "operation": None},
            "after": {"entity_type": recovered_entity, "operation": recovered_operation, "filters": filters},
        })
    # Some smaller local models expose a complete IFC entity and operation in
    # their typed rationale but default `source`/`intent` to enum fallback
    # values. This is an unambiguous schema correction, not language routing.
    if plan.source == "unsupported" and plan.entity_type and plan.operation in {"count", "list", "group_by", "get_properties", "inspect_relationship"}:
        intent_by_operation = {
            "count": "count", "list": "list", "group_by": "aggregate",
            "get_properties": "property_lookup", "inspect_relationship": "relationship_lookup",
        }
        plan = plan.model_copy(update={"source": "ifc", "intent": intent_by_operation[plan.operation]})
        corrections.append({
            "correction": "recovered IFC source and intent from explicit typed entity and operation",
            "before": {"source": "unsupported", "intent": "unsupported"},
            "after": {"source": "ifc", "intent": intent_by_operation[plan.operation]},
        })
    legacy_group_by = filters.get("group_by")
    if legacy_group_by and plan.group_by in (None, "none"):
        filters.pop("group_by", None)
        plan = plan.model_copy(update={
            "filters": filters,
            "group_by": legacy_group_by,
            "operation": "group_by",
            "intent": "aggregate",
            "expected_result_shape": "grouped_counts",
        })
        corrections.append({
            "correction": "moved filters.group_by to canonical group_by field",
            "before": {"filters": {"group_by": legacy_group_by}, "group_by": None},
            "after": {"filters": {}, "group_by": legacy_group_by, "operation": "group_by"},
        })
    return _apply_expected_shape(plan), corrections


def canonicalize_multi_plan(multi_plan: MultiQueryPlan, context: dict[str, Any] | None = None) -> tuple[MultiQueryPlan, list[dict[str, Any]]]:
    corrected: list[QueryPlan] = []
    events: list[dict[str, Any]] = []
    for plan in multi_plan.subplans:
        canonical, changes = canonicalize_subplan(plan, context)
        corrected.append(canonical)
        events.extend([{"subtask_id": canonical.subtask_id, **change} for change in changes])
    reconciled, reconciliation_events = _reconcile_model_declared_semantics(corrected, multi_plan, context or {})
    events.extend(reconciliation_events)
    return multi_plan.model_copy(update={"subplans": reconciled}), events


def calibrate_interpretation_confidence(multi_plan: MultiQueryPlan) -> tuple[MultiQueryPlan, list[dict[str, Any]]]:
    """Resolve a contradiction in the *model-produced* interpretation contract.

    A small local model can correctly emit a normalized request, a supported
    typed IFC plan, and a reversible correction, while still leaving its
    default ``requires_clarification`` flag set.  This function never examines
    the raw user language.  It only permits a medium-confidence continuation
    when the model's own structured interpretation has exactly one executable,
    low-risk meaning.  Otherwise the clarification flag remains authoritative.
    """
    executable = bool(multi_plan.subplans) and all(
        plan.source == "ifc"
        and plan.entity_type is not None
        and plan.operation == "count"
        and plan.group_by in (None, "none")
        and plan.postprocess in (None, "none")
        and plan.expected_result_shape == "scalar_count"
        for plan in multi_plan.subplans
    )
    if not (
        multi_plan.requires_clarification
        and multi_plan.normalized_request
        and multi_plan.corrections
        and executable
    ):
        return multi_plan, []

    calibrated = multi_plan.model_copy(update={
        "requires_clarification": False,
        "interpretation_confidence": "medium",
    })
    return calibrated, [{
        "correction": "resolved contradictory clarification flag from the model's complete typed interpretation",
        "before": {
            "requires_clarification": True,
            "interpretation_confidence": multi_plan.interpretation_confidence,
        },
        "after": {
            "requires_clarification": False,
            "interpretation_confidence": "medium",
        },
    }]


def _reconcile_model_declared_semantics(plans: list[QueryPlan], multi_plan: MultiQueryPlan, context: dict[str, Any]) -> tuple[list[QueryPlan], list[dict[str, Any]]]:
    """Restore fields the model explicitly states in its normalized meaning.

    This deliberately examines only model-produced English semantic artifacts
    (`normalized_request` and `rationale`), never the raw end-user language.
    It protects against local models returning duplicated placeholder
    subplans despite correctly describing the intended domain entities.
    """
    # An explicit unsupported disposition is terminal.  Do not recover entity
    # words from the explanatory rationale (for example, "rooms") into an
    # executable fallback plan; that would turn an understood unsupported
    # request into an unrelated count operation.
    if multi_plan.intent == "unsupported" or any(plan.intent == "unsupported" for plan in plans):
        return plans, []
    declared_text = " ".join(filter(None, [multi_plan.normalized_request, multi_plan.rationale, *(plan.rationale for plan in plans)]))
    declared_entities = list(dict.fromkeys(re.findall(r"\bIfc(?:Door|Window|Wall|Space|Stair|Slab)\b", declared_text)))
    # The planner's normalized request is an internal semantic artefact, not
    # raw end-user language.  Smaller local models sometimes state "doors"
    # there while leaving the enum field empty.  Recover only this finite IFC
    # ontology vocabulary so an already-understood grouped request cannot be
    # downgraded to an unsupported refusal.
    if not declared_entities:
        semantic_entity_words = {
            "doors": "IfcDoor", "door": "IfcDoor",
            "windows": "IfcWindow", "window": "IfcWindow",
            "walls": "IfcWall", "wall": "IfcWall",
            "spaces": "IfcSpace", "space": "IfcSpace", "rooms": "IfcSpace", "room": "IfcSpace",
            "stairs": "IfcStair", "stair": "IfcStair",
            "slabs": "IfcSlab", "slab": "IfcSlab",
        }
        semantic_lower = declared_text.lower()
        declared_entities = [entity for word, entity in semantic_entity_words.items() if re.search(rf"\b{word}\b", semantic_lower)]
        declared_entities = list(dict.fromkeys(declared_entities))
    if not declared_entities:
        return plans, []
    lowered = declared_text.lower()
    asks_grouped = any(phrase in lowered for phrase in ("each floor", "per floor", "each storey", "per storey", "by storey", "on every floor", "floor with the most", "storey with the most"))
    asks_argmax = any(phrase in lowered for phrase in ("which floor has the most", "which storey has the most", "floor with the most", "storey with the most", "most windows", "most doors"))
    target_entities = declared_entities if len(declared_entities) > 1 else [plans[0].entity_type or declared_entities[0]]
    repeated_or_missing_entities = len({plan.entity_type for plan in plans if plan.entity_type}) <= 1
    if (
        len(target_entities) == len(plans)
        or (len(target_entities) > 1 and repeated_or_missing_entities)
        or (asks_grouped and len(target_entities) == 1 and repeated_or_missing_entities)
    ):
        output: list[QueryPlan] = []
        events: list[dict[str, Any]] = []
        for index, entity_type in enumerate(target_entities, start=1):
            plan = plans[min(index - 1, len(plans) - 1)]
            updates: dict[str, Any] = {"entity_type": entity_type, "subtask_id": plan.subtask_id or f"task_{index}"}
            if plan.source == "unsupported":
                updates["source"] = "ifc"
            if asks_grouped:
                updates.update({"operation": "group_by", "intent": "aggregate", "group_by": "storey", "postprocess": "argmax" if asks_argmax else None, "expected_result_shape": "grouped_counts"})
            elif plan.operation is None:
                updates.update({"operation": "count", "intent": "count", "expected_result_shape": "scalar_count"})
            if context.get("active_storey") and any(marker in lowered for marker in ("active storey", "that floor", "that storey", "filter on storey")):
                updated_filters = dict(plan.filters)
                updated_filters.setdefault("storey", context["active_storey"])
                updates["filters"] = updated_filters
            restored = plan.model_copy(update=updates)
            if restored.model_dump() != plan.model_dump() or len(target_entities) != len(plans):
                events.append({"subtask_id": restored.subtask_id, "correction": "reconciled typed plan with model-declared normalized semantics", "before": {"entity_type": plan.entity_type, "operation": plan.operation, "group_by": plan.group_by}, "after": {"entity_type": restored.entity_type, "operation": restored.operation, "group_by": restored.group_by, "filters": restored.filters}})
            output.append(_apply_expected_shape(restored))
        return output, events
    return plans, []


def _apply_expected_shape(plan: QueryPlan) -> QueryPlan:
    if plan.group_by and plan.group_by != "none":
        return plan.model_copy(update={"operation": "group_by", "intent": "aggregate", "expected_result_shape": "grouped_counts"})
    shape_by_operation = {
        "count": "scalar_count", "list": "list", "get_properties": "properties",
        "extract_field": "document_value", "inspect_view": "visual_claim",
        "inspect_relationship": "properties",
        "aggregate_quantity": "scalar_measurement",
        "max_window_height": "scalar_measurement",
        "space_distance": "scalar_measurement",
    }
    if plan.operation in shape_by_operation and not plan.expected_result_shape:
        return plan.model_copy(update={"expected_result_shape": shape_by_operation[plan.operation]})
    return plan


def validate_multi_plan(multi_plan: MultiQueryPlan) -> list[PlanIssue]:
    """Validate semantic relationships that JSON/Pydantic cannot express."""
    issues: list[PlanIssue] = []
    for plan in multi_plan.subplans:
        modifiers = EXECUTION_MODIFIERS.intersection(plan.filters)
        if modifiers:
            issues.append(PlanIssue(plan.subtask_id, "execution_modifier_in_filters", f"filters contains execution modifiers: {sorted(modifiers)}"))
        grouped = bool(plan.group_by and plan.group_by != "none")
        if grouped and plan.operation != "group_by":
            issues.append(PlanIssue(plan.subtask_id, "grouping_operation_mismatch", "A grouped plan must use operation=group_by."))
        expected_group_shape = "single_group_extremum" if plan.postprocess in {"argmax", "argmin"} else "grouped_counts"
        if grouped and plan.expected_result_shape not in {expected_group_shape, "grouped_counts" if plan.postprocess in {"argmax", "argmin"} else expected_group_shape}:
            issues.append(PlanIssue(plan.subtask_id, "grouped_shape_missing", f"A grouped plan must expect {expected_group_shape}."))
        if plan.postprocess in {"argmax", "argmin"} and not grouped:
            issues.append(PlanIssue(plan.subtask_id, "comparison_requires_grouping", "argmax/argmin requires a grouping dimension."))
        if plan.source == "ifc" and plan.intent not in {"clarification", "unsupported"} and not plan.entity_type:
            issues.append(PlanIssue(plan.subtask_id, "missing_entity", "An IFC query requires an entity_type."))
        if plan.source == "ifc" and not plan.operation:
            issues.append(PlanIssue(plan.subtask_id, "missing_operation", "An IFC query requires an operation."))
    return issues


def eligible_scalar_count_batch(plans: list[QueryPlan]) -> tuple[bool, str]:
    """Batch only exact equivalent scalar counts; never erase semantics."""
    if len(plans) < 2:
        return False, "A batch requires at least two subplans."
    baseline = plans[0]
    if not all(item.source == "ifc" and item.operation == "count" for item in plans):
        return False, "Only scalar IFC count operations are batchable."
    semantics = (baseline.filters, baseline.group_by, baseline.postprocess, baseline.expected_result_shape)
    if any((item.filters, item.group_by, item.postprocess, item.expected_result_shape) != semantics for item in plans):
        return False, "Subplans do not share all execution semantics."
    if baseline.group_by not in (None, "none") or baseline.postprocess not in (None, "none") or baseline.expected_result_shape != "scalar_count":
        return False, "Grouping, postprocessing, or non-scalar result shape prevents scalar batching."
    return True, "Equivalent scalar IFC counts may be batched."


def actual_result_shape(value: Any) -> str:
    if isinstance(value, dict) and "value_m" in value:
        return "scalar_measurement"
    if isinstance(value, dict):
        return "grouped_counts"
    if isinstance(value, int):
        return "scalar_count"
    if isinstance(value, list):
        return "list"
    return "unknown"


def verify_execution_consistency(plan: QueryPlan, *, tool_query: dict[str, Any], result_value: Any, answer: str) -> list[PlanIssue]:
    """Ensure the executed query and rendered answer retain the validated intent."""
    issues: list[PlanIssue] = []
    # Property lookup is intentionally represented as a bounded list of
    # records by the repository.  Its semantic result shape is ``properties``
    # rather than a generic list, so selected-element identity cannot be
    # rejected by an unrelated list contract.
    observed_shape = plan.expected_result_shape if plan.operation == "get_properties" and plan.expected_result_shape in {"element_identity", "element_storey", "element_properties"} and isinstance(result_value, list) else "properties" if plan.operation == "get_properties" and isinstance(result_value, list) else actual_result_shape(result_value)
    if plan.expected_result_shape == "single_group_extremum" and plan.postprocess in {"argmax", "argmin"} and observed_shape == "grouped_counts":
        observed_shape = "single_group_extremum"
    if plan.expected_result_shape and plan.expected_result_shape != observed_shape:
        issues.append(PlanIssue(plan.subtask_id, "result_shape_mismatch", f"Expected {plan.expected_result_shape}; tool returned {observed_shape}."))
    if plan.group_by and plan.group_by != "none":
        if tool_query.get("group_by") != plan.group_by or tool_query.get("operation") != "group_by":
            issues.append(PlanIssue(plan.subtask_id, "execution_does_not_preserve_grouping_requirement", "Tool invocation did not preserve the grouping requirement."))
        if not isinstance(result_value, dict):
            issues.append(PlanIssue(plan.subtask_id, "grouped_result_missing", "Grouped query returned a scalar result."))
        if not answer.strip():
            issues.append(PlanIssue(plan.subtask_id, "empty_answer", "The verified result was not rendered."))
    if plan.postprocess in {"argmax", "argmin"}:
        if not isinstance(result_value, dict) or not result_value:
            issues.append(PlanIssue(plan.subtask_id, "postprocess_requires_grouped_result", "A grouped result is required before argmax/argmin can be applied."))
        else:
            chooser = max if plan.postprocess == "argmax" else min
            winner, count = chooser(result_value.items(), key=lambda item: (item[1], item[0]))
            if winner not in answer or str(count) not in answer:
                issues.append(PlanIssue(plan.subtask_id, f"postprocess_{plan.postprocess}_not_executed", "The rendered answer does not identify the deterministic grouped winner and count."))
    return issues
