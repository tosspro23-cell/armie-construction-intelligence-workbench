from __future__ import annotations

import re
from typing import Any

from app.schemas.models import MultiQueryPlan, QueryPlan
from app.tools.document.analyzer import DocumentAnalyzer

ELEMENT_ALIASES = {
    "door": "IfcDoor", "doors": "IfcDoor",
    "window": "IfcWindow", "windows": "IfcWindow",
    "wall": "IfcWall", "walls": "IfcWall",
    "space": "IfcSpace", "spaces": "IfcSpace",
    "stair": "IfcStair", "stairs": "IfcStair",
    "slab": "IfcSlab", "slabs": "IfcSlab",
    "opening": "IfcWindow", "openings": "IfcWindow",
    "glazed opening": "IfcWindow", "glazed openings": "IfcWindow",
}
SUPPORTED_ENTITY_TYPES = set(ELEMENT_ALIASES.values())


def cross_source_join_requested(question: str) -> bool:
    """Detect an explicit request to combine drawing and IFC semantics.

    This is an intent-level safety boundary, not a route for either source.
    """
    lowered = " ".join(question.lower().split())
    join_marker = re.search(r"\b(join|combine|merge|link|cross[- ]source|relate)\b|关联|连接|合并", lowered)
    pdf_marker = re.search(r"\b(pdf|drawing|load|connected load|circuit|diversity|schedule)\b|图纸|负载|配电", lowered)
    ifc_marker = re.search(r"\b(ifc|bim|room|space|area|model)\b|房间|空间|面积|模型", lowered)
    return bool(join_marker and pdf_marker and ifc_marker)


def nearest_space_requested(question: str) -> bool:
    """Recognise nearest-room intent before the capability gate."""
    lowered = " ".join(question.lower().split())
    return bool(re.search(r"\b(closest|nearest)\b", lowered) and re.search(r"\b(room|space)\b", lowered))
DEICTIC_TERMS = (
    "what is this", "this one", "this element", "what properties does it have",
    "which storey is it", "which floor is it", "which level is it", "where is it located", "what material is this",
    "这个是什么", "当前选中的是什么", "它在哪一层", "这个构件有什么属性",
)


def selected_element_plan(question: str, context: dict[str, Any]) -> QueryPlan | None:
    """Build the canonical deterministic equivalent of ``identify_element``.

    The public IFC tool contract exposes identity as a narrowly filtered
    ``get_properties`` operation.  It is deliberately constructed before a
    stale conversational target can influence semantic planning.
    """
    if not context.get("active_entity_ids"):
        return None
    lowered = question.lower()
    if not any(term in lowered for term in DEICTIC_TERMS):
        return None
    entity_type = context.get("active_entity_type")
    if not entity_type:
        return None
    filters: dict[str, Any] = {"global_ids": list(context["active_entity_ids"])}
    if context.get("active_express_ids"):
        filters["express_ids"] = list(context["active_express_ids"])
    shape = "element_storey" if any(term in lowered for term in ("which storey", "which floor", "which level", "哪一层", "哪层")) else "element_properties" if any(term in lowered for term in ("properties", "属性")) else "element_identity"
    return QueryPlan(
        subtask_id="task_1",
        source="ifc",
        intent="property_lookup",
        operation="get_properties",
        entity_type=entity_type,
        filters=filters,
        group_by="none",
        expected_result_shape=shape,
        rationale="The latest deictic request is grounded in the active viewer selection.",
        planning_mode="context",
        rule_id="selected_element_precedence",
        matched_signals=["reference:selected_element", "selection:global_id"],
        match_status="complete",
    )


def resolve_reference(question: str, context: dict) -> tuple[str, dict, str | None]:
    """Pass language-neutral structured context to the semantic planner.

    Reference resolution is deliberately *not* implemented with pronoun or
    language lookup tables.  A model receives this state when a request is not
    a fully-covered narrow English fast path.
    """
    return question, dict(context), None


def fast_path_coverage(question: str, context: dict, has_viewer_context: bool) -> dict[str, Any]:
    """Decide whether the deterministic shortcut covers the *whole* request.

    This accepts only deliberately narrow English templates.  Any non-ASCII,
    compound, elliptical, mixed-language, or contextual request is routed to
    the typed semantic planner rather than partially executing a keyword hit.
    """
    normalized = " ".join(question.strip().lower().split())
    ascii_only = question.isascii()
    entities = [key for key in ELEMENT_ALIASES if re.search(rf"\b{re.escape(key)}\b", normalized)]
    unique_entities = sorted({ELEMENT_ALIASES[key] for key in entities})
    simple_patterns = (
        r"how many (doors|windows|walls|spaces|stairs|slabs|openings|glazed openings) (are )?(there )?(in (the )?project)?\??",
        r"count (the )?(doors|windows|walls|spaces|stairs|slabs|openings|glazed openings)\.?",
        r"which (floor|storey|level) has the (most|greatest number of|highest count of) windows\??",
        r"what is the (maximum|minimum) (height|width|length|area) of (the )?(doors|windows|walls|slabs|stairs)( in (the |this )?project)?\??",
        r"what is the (maximum|minimum) (door|window|wall|slab|stair) (height|width|length|area)\??",
        r"what is the distance from .+ to .+\??",
    )
    selected_element_template = bool(context.get("active_entity_ids")) and normalized in {"what is this?", "what is this", "which floor is it on?", "which floor is it on", "which level is it on?", "which level is it on"}
    clarification_template = bool(context.get("pending_space_distance")) and bool(re.fullmatch(r"(?:use|choose|select)\s+(?:bedroom\s+)?[a-z0-9_-]+\.?", normalized))
    grouping_markers = ("by floor", "by storey", "each floor", "each storey", "per floor", "per storey", "break them down", "break down", "every floor")
    prior_entities = {item.get("entity_type") for item in context.get("previous_subplans", []) if item.get("source") == "ifc" and item.get("entity_type")}
    simple_template = any(re.fullmatch(pattern, normalized) for pattern in simple_patterns) or selected_element_template or clarification_template
    generic_count = bool(re.search(r"\b(how many|count|number of|give me the number|there are)\b", normalized)) and len(unique_entities) == 1
    generic_grouping = bool(unique_entities or prior_entities) and any(marker in normalized for marker in grouping_markers)
    complete = ascii_only and (simple_template or generic_count or generic_grouping)
    covered = unique_entities if complete else []
    unresolved: list[str] = [] if complete else ["semantic_decomposition_required"]
    return {
        "coverage_status": "complete" if complete else "incomplete",
        "covered_intents": covered,
        "unresolved_intents": unresolved,
        "reason": None if complete else "heuristic_coverage_incomplete",
        "has_viewer_context": has_viewer_context,
    }


def heuristic_multi_plan(question: str, context: dict, has_viewer_context: bool, source_preference: str = "auto") -> MultiQueryPlan | None:
    """Build a generic multi-entity IFC count/grouping plan.

    This parses bounded construction semantics, not project-specific
    sentences. Unsupported or compound non-count requests remain on the LLM
    planner path.
    """
    if source_preference != "auto":
        return None
    normalized = " ".join(question.strip().lower().split())
    if cross_source_join_requested(question):
        return MultiQueryPlan(
            intent="unsupported", response_language="en", raw_user_message=question,
            normalized_request=question, interpretation_confidence="high", subplans=[],
            rationale="Cross-source joins between engineering-drawing and IFC semantics are outside this release.",
        )
    argmax = bool(re.search(r"\b(most|greatest number|highest count)\b", normalized))
    argmin = bool(re.search(r"\b(fewest|lowest count|least)\b", normalized))
    # Keep the deterministic route deliberately narrow.  Merely mentioning a
    # level in a natural-language comparison (for example, “one level at a
    # time”) is not enough to prove that the heuristic has recovered the
    # complete grouped-argmax contract.  Such requests must fall through to
    # the semantic planner, which can return the same canonical typed plan
    # after interpreting the whole utterance.
    grouping = any(marker in normalized for marker in ("by floor", "by storey", "by level", "each floor", "each storey", "each level", "per floor", "per storey", "per level", "break them down", "break down", "every floor", "which floor", "which storey", "which level"))
    count_intent = bool(re.search(r"\b(how many|count|number of|give me the number|there are)\b", normalized))
    entity_types: list[str] = []
    for alias, entity_type in ELEMENT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized) and entity_type not in entity_types:
            entity_types.append(entity_type)
    if not entity_types and grouping:
        for item in context.get("previous_subplans", []):
            entity_type = item.get("entity_type")
            if item.get("source") == "ifc" and entity_type and entity_type not in entity_types:
                entity_types.append(entity_type)
    if not entity_types and grouping:
        entity_types = [item.get("entity_type") for item in context.get("previous_subplans", []) if item.get("source") == "ifc" and item.get("entity_type")]
        entity_types = list(dict.fromkeys(entity_types))
    if not entity_types or (not count_intent and not grouping):
        return None
    response_language = "zh-CN" if any("\u4e00" <= char <= "\u9fff" for char in question) else "en"
    subplans = []
    for index, entity_type in enumerate(entity_types, start=1):
        operation = "group_by" if grouping else "count"
        postprocess = "argmax" if argmax else "argmin" if argmin else None
        subplans.append(QueryPlan(
            subtask_id=f"task_{index}", source="ifc", intent="aggregate", operation=operation,
            entity_type=entity_type, filters={}, group_by="storey" if grouping else "none",
            # Grouped extrema retain the full grouped result for deterministic
            # grading, synthesis, and evidence; postprocess identifies the
            # winning group without changing the result shape contract.
            postprocess=postprocess, expected_result_shape="grouped_counts" if grouping else "scalar_count",
            rationale="Explicit construction entities and count/grouping intent converge on the canonical IFC plan.",
            planning_mode="heuristic", rule_id="generic_multi_entity_ifc", matched_signals=[f"entity:{entity_type}", f"operation:{operation}"], match_status="complete",
        ))
    return MultiQueryPlan(
        intent="multi_query" if len(subplans) > 1 else "single_query", response_language=response_language,
        raw_user_message=question, normalized_request=question, interpretation_confidence="high", subplans=subplans,
        rationale="Generic multi-entity IFC count/grouping plan.",
    )


def _signals(question: str, entity_type: str | None, operation: str, group_by: str | None) -> list[str]:
    lowered = question.lower()
    signals = []
    if entity_type:
        signals.append(f"entity:{entity_type}")
    if operation:
        signals.append(f"operation:{operation}")
    if group_by and group_by != "none":
        signals.append(f"grouping:{group_by}")
    if "how many" in lowered:
        signals.append("phrase:how_many")
    return signals


def heuristic_plan(question: str, context: dict, has_viewer_context: bool, source_preference: str = "auto") -> QueryPlan:
    """Return a complete fast-path plan or an explicit partial/unknown plan.

    This intentionally handles only recognisable requests. A partial/unknown
    result is a signal to invoke the semantic planner rather than a refusal.
    """
    lowered = question.lower().strip()
    # A PDF or viewer override fully determines the source and operation.  An
    # IFC override, however, must still preserve the question's operation
    # (for example a storey grouping); treating every IFC override as `count`
    # makes valid multi-turn questions silently lose their semantics.
    if source_preference in {"pdf", "viewer_snapshot"}:
        return QueryPlan(
            source=source_preference, intent="document_lookup" if source_preference == "pdf" else "visual_inspection",
            operation="extract_field" if source_preference == "pdf" else "inspect_view",
            requested_field=DocumentAnalyzer.target_field(question) if source_preference == "pdf" else None,
            entity_type=context.get("active_entity_type"), filters=context.get("active_filters", {}), group_by="none",
            rationale="The user applied a manual source override.", planning_mode="context", rule_id="source_override",
            matched_signals=[f"source_override:{source_preference}"], match_status="complete",
        )

    if cross_source_join_requested(question):
        return QueryPlan(
            source="unsupported", intent="unsupported", operation=None,
            rationale="Cross-source joins between engineering-drawing and IFC semantics are outside this release.",
            planning_mode="heuristic", rule_id="cross_source_join_guard", matched_signals=["intent:cross_source_join"], match_status="complete",
        )
    if any(term in lowered for term in ("load", "circuit", "diversity", "schedule", "breaker", "electrical")):
        return QueryPlan(
            source="pdf", intent="document_lookup", operation="extract_field", requested_field=DocumentAnalyzer.target_field(question),
            rationale="Question contains explicit engineering-drawing terminology.", planning_mode="heuristic",
            rule_id="pdf_engineering_schedule", matched_signals=["domain:electrical_schedule"], match_status="complete",
        )
    quantity_match = re.fullmatch(
        r"what is the (maximum|minimum) (height|width|length|area) of (the )?(doors|windows|walls|slabs|stairs)( in (the |this )?project)?\??",
        lowered,
    ) or re.fullmatch(r"what is the (maximum|minimum) (door|window|wall|slab|stair) (height|width|length|area)\??", lowered)
    paraphrase_quantity = re.fullmatch(r"(?:which|what) (door|window|wall|slab|stair) has the (greatest|highest|maximum|smallest|lowest|minimum) (height|width|length|area)[?.]?", lowered) or re.fullmatch(r"find the (tallest|shortest) (door|window|wall|slab|stair)(?: in the building| in the project)?[?.]?", lowered)
    if paraphrase_quantity:
        if lowered.startswith("find the"):
            qualifier, entity_word = paraphrase_quantity.group(1), paraphrase_quantity.group(2)
            measure_word = "height"
        else:
            entity_word, qualifier, measure_word = paraphrase_quantity.group(1), paraphrase_quantity.group(2), paraphrase_quantity.group(3)
        if qualifier in {"tallest", "shortest"}:
            aggregation = "max" if qualifier == "tallest" else "min"
            measure = "height"
        else:
            aggregation = "max" if qualifier in {"greatest", "highest", "maximum"} else "min"
            measure = measure_word
        entity_type = ELEMENT_ALIASES[entity_word]
        return QueryPlan(source="ifc", intent="aggregate", operation="aggregate_quantity", entity_type=entity_type, filters={}, group_by="none", postprocess=None, expected_result_shape="scalar_measurement", measure=measure, aggregation=aggregation, rationale="Controlled quantity extremum paraphrase mapped to the canonical IFC measurement contract.", planning_mode="heuristic", rule_id="generic_quantity_aggregation_paraphrase", matched_signals=[f"measure:{measure}", f"aggregation:{aggregation}"], match_status="complete")
    if quantity_match:
        groups = quantity_match.groups()
        aggregation = "max" if groups[0] == "maximum" else "min"
        measure = groups[1] if groups[1] in {"height", "width", "length", "area"} else groups[2]
        entity_word = groups[3] if groups[1] in {"height", "width", "length", "area"} else groups[1]
        entity_type = ELEMENT_ALIASES[entity_word.rstrip("s") if entity_word.rstrip("s") in ELEMENT_ALIASES else entity_word]
        return QueryPlan(
            source="ifc", intent="aggregate", operation="aggregate_quantity", entity_type=entity_type,
            filters={}, group_by="none", postprocess=None, expected_result_shape="scalar_measurement",
            measure=measure, aggregation=aggregation,
            rationale="Explicit entity, controlled measure, and aggregation converge on the generic IFC quantity contract.",
            planning_mode="heuristic", rule_id="generic_quantity_aggregation", matched_signals=[f"measure:{measure}", f"aggregation:{aggregation}"], match_status="complete",
        )
    distance_match = re.search(r"(?:distance|far) from (.+?) to (.+?)(?:\?|$)", lowered)
    if context.get("pending_space_distance") and re.search(r"(?:use|choose|select)\s+(?:bedroom\s+)?[A-Za-z0-9_-]+", lowered):
        identifier = re.search(r"(?:use|choose|select)\s+(?:bedroom\s+)?([A-Za-z0-9_-]+)", lowered).group(1)
        pending = dict(context["pending_space_distance"])
        return QueryPlan(
            source="ifc", intent="relationship_lookup", operation="space_distance", entity_type="IfcSpace",
            filters={"from_space": identifier, "to_space": pending.get("to_space", "Main Kitchen")},
            expected_result_shape="scalar_measurement", rationale="Clarification supplied the missing source space identifier.",
            planning_mode="context", rule_id="space_distance_clarification", matched_signals=["reference:pending_space_distance"], match_status="complete",
        )
    if distance_match:
        from_space = re.sub(r"^the\s+", "", distance_match.group(1).strip())
        to_space = re.sub(r"^the\s+", "", distance_match.group(2).strip())
        # Generic room labels intentionally remain ambiguous; the repository
        # will require an exact room identifier before measuring.
        return QueryPlan(
            source="ifc", intent="relationship_lookup", operation="space_distance", entity_type="IfcSpace",
            filters={"from_space": from_space, "to_space": to_space}, expected_result_shape="scalar_measurement",
            rationale="A generic space-distance request requires two uniquely resolved named spaces.", planning_mode="heuristic",
            rule_id="generic_space_distance", matched_signals=["capability:space_distance"], match_status="complete",
        )
    if any(term in lowered for term in ("open or enclosed", "what can you see", "in this view", "current view", "visible")):
        return QueryPlan(
            source="viewer_snapshot", intent="visual_inspection", operation="inspect_view",
            rationale="Question explicitly requests current-view visual inspection.", planning_mode="heuristic",
            rule_id="viewer_snapshot_phrase", matched_signals=["source:current_view"],
            match_status="complete" if has_viewer_context else "partial",
        )

    entity_type = next((value for key, value in ELEMENT_ALIASES.items() if re.search(rf"\b{re.escape(key)}\b", lowered)), None)
    is_deictic = any(term in lowered for term in DEICTIC_TERMS)
    if is_deictic and context.get("active_entity_type"):
        return QueryPlan(
            source="ifc", intent="property_lookup", operation="get_properties",
            entity_type=context["active_entity_type"], filters=context.get("active_filters", {}), group_by="none",
            rationale="Question is grounded in the currently selected IFC element.", planning_mode="context",
            rule_id="selected_element_property_lookup", matched_signals=["reference:selected_element"], match_status="complete",
        )

    # `resolve_reference` records an explicit short-lived context when a user
    # says "break that down by room".  The follow-up does not repeat the IFC
    # entity name, so recover it from the prior deterministic plan instead of
    # treating a valid conversation turn as an unsupported standalone prompt.
    recognises_ifc = bool(entity_type or any(term in lowered for term in ("storey", "floor", "level", "bim", "ifc", "property", "quantity")))
    if recognises_ifc:
        grouping_language = any(term in lowered for term in ("by floor", "by storey", "by level", "per floor", "per storey", "per level", "each floor", "each storey", "each level"))
        operation = "group_by" if grouping_language else "count" if any(term in lowered for term in ("how many", "count", "number of", "total number", "total count")) else "group_by"
        if any(term in lowered for term in ("maximum", "max ", "highest")):
            operation = "max"
        elif any(term in lowered for term in ("minimum", "min ", "lowest")):
            operation = "min"
        elif any(term in lowered for term in ("average", "mean")):
            operation = "average"
        elif any(term in lowered for term in ("total", "sum")) and not any(term in lowered for term in ("total number", "total count")):
            operation = "sum"
        elif any(term in lowered for term in ("property", "properties")):
            operation = "get_properties"
        group_by = "storey" if any(term in lowered for term in ("which floor", "which storey", "which level", "by floor", "by storey", "across levels", "by level", "per floor", "per level", "per storey", "each floor", "each level", "each storey")) else "none"
        postprocess = "argmax" if group_by == "storey" and any(term in lowered for term in ("most", "maximum", "highest", "greatest number")) else "argmin" if group_by == "storey" and any(term in lowered for term in ("fewest", "least", "minimum", "lowest")) else None
        if context.get("active_group_by") == "space":
            group_by, operation = "space", "group_by"
        explicit_grouping = any(term in lowered for term in ("most", "greatest number", "highest count", "fewest", "lowest count", "by floor", "by storey", "per floor", "per storey", "each floor", "each storey", "by level", "per level"))
        complete = bool(entity_type) and (operation == "count" or operation == "get_properties" or (group_by != "none" and explicit_grouping))
        return QueryPlan(
            source="ifc", intent="aggregate" if operation in {"min", "max", "sum", "average", "group_by"} else operation,
            operation=operation, entity_type=entity_type or context.get("active_entity_type"), group_by=group_by, postprocess=postprocess,
            expected_result_shape=("single_group_extremum" if postprocess else "grouped_counts" if group_by != "none" else "scalar_count" if operation == "count" else None),
            filters=context.get("active_filters", {}), rationale="Question maps to constrained IFC operations.",
            planning_mode="heuristic", rule_id="ifc_entity_operation" if complete else None,
            matched_signals=_signals(question, entity_type, operation, group_by), match_status="complete" if complete else "partial",
        )
    return QueryPlan(
        source="unsupported", intent="unsupported", operation=None,
        rationale="No complete deterministic fast-path match was found.", planning_mode="heuristic",
        matched_signals=[], match_status="unknown",
    )


def capability_gate(plan: QueryPlan) -> tuple[bool, str | None]:
    if plan.source == "unsupported":
        return False, plan.rationale or "No supported source or operation was identified."
    if plan.source == "ifc":
        if not plan.entity_type:
            return False, "The IFC plan does not identify an entity type."
        if plan.entity_type not in SUPPORTED_ENTITY_TYPES:
            return False, f"The constrained IFC tool does not support {plan.entity_type}."
        if plan.operation in {"min", "max", "sum", "average", "aggregate_quantity"} and plan.operation != "aggregate_quantity":
            return False, "Numeric IFC aggregation requires the canonical aggregate_quantity contract."
        if plan.operation == "aggregate_quantity" and (plan.measure not in {"height", "width", "length", "area"} or plan.aggregation not in {"min", "max"}):
            return False, "Quantity aggregation requires a controlled measure and min/max aggregation."
        if plan.operation == "space_distance" and plan.entity_type != "IfcSpace":
            return False, "Space distance is only supported between explicitly resolved IFC spaces."
        if plan.group_by == "space":
            # Execution will make the source-data distinction once relationships are inspected.
            return True, None
    return True, None


def ground_plan_to_selection(plan: QueryPlan, context: dict, question: str) -> tuple[QueryPlan, bool]:
    """Inject stable IFC IDs into a selected-element query and preserve identity."""
    selected_ids = context.get("active_entity_ids", [])
    if plan.source != "ifc" or not selected_ids:
        return plan, False
    selected_plan = selected_element_plan(question, context)
    if not selected_plan:
        return plan, False
    return selected_plan, True


def planner_prompt(question: str, context: dict, source_preference: str) -> str:
    return f"""You are a semantic planner for a construction intelligence system.
Interpret language only; never calculate, invent IFC facts, or use unsupported geometry.
Return a MultiQueryPlan-compatible JSON object. Use only these IFC entity types: {sorted(SUPPORTED_ENTITY_TYPES)}.
Supported IFC operations are count, list, group_by, get_properties, inspect_relationship, aggregate_quantity, and a bounded space_distance.
Identify every requested task, including compound tasks, and create one independent subplan per task.
Do not omit requested entities, operations, filters, comparisons, sources, or contextual references.
Populate raw_user_message, normalized_request, interpretation_confidence, corrections, and requires_clarification.
Keep the raw message unchanged. Normalize only when context and the supported construction ontology make the intended meaning clear.
Never use execution modifiers inside filters. `group_by` is top-level only. For all grouped requests use operation="group_by"
and expected_result_shape="grouped_counts". For "most"/"least" keep the grouped tool result and set postprocess="argmax"/"argmin".
Use response_language for the latest user message. Resolve follow-ups using Conversation context and viewer selection,
not keyword or pronoun tables. Map glazed openings/openings to IfcWindow and levels/floors/storeys to group_by=storey.
For a room breakdown only use group_by=space; source execution will honestly clarify if IFC relationships are absent.
Use source pdf for electrical schedule/load/circuit questions and viewer_snapshot only for visual/current-view questions.
For unknown or unsupported requests, use source=unsupported and intent=unsupported.

Semantic examples (illustrations, not keyword rules):
1. A request to count both doors and windows has two IFC count subplans: IfcDoor and IfcWindow.
2. A request to count doors and identify the floor/storey with the most windows has two subplans: count IfcDoor, and group_by storey for IfcWindow.
3. A Chinese request asking for door and window counts has the same two subplans and response_language="zh-CN".
4. A punctuation-free dictation request which asks for doors, windows, and the floor with most windows has three subplans: count IfcDoor, count IfcWindow, group_by storey for IfcWindow.
5. When Conversation context contains a verified active_storey and a follow-up asks for doors on that level in any language, create an IfcDoor count subplan with filters.storey=active_storey.
6. When a selected entity is relevant, preserve its GlobalId filter; do not invent identifiers.
7. A request asking only which floor/storey has the most windows needs exactly one IfcWindow group_by=storey subplan. Do not add a project-wide window count unless the user explicitly requested it.
8. A follow-up asking "each level" after verified door/window counts preserves both entities and replaces the scalar operation with group_by=storey.
9. Obvious speech-to-text substitutions should be interpreted from the construction ontology when high confidence, recorded in corrections, and not handled by keyword/typo tables. If two supported meanings are similarly plausible, set requires_clarification=true.
10. For controlled quantity extrema use source=ifc, operation=aggregate_quantity, entity_type from the supported IFC ontology, measure in {{height,width,length,area}}, aggregation in {{min,max}}, and expected_result_shape=scalar_measurement.
11. For room-to-room distance use source=ifc, entity_type=IfcSpace, operation=space_distance and filters.from_space / filters.to_space. It is horizontal centroid distance only. If a generic room name resolves to multiple spaces, request clarification rather than choosing one.

Return actual typed subplans, never an explanation of what the system could do.
Question: {question}
Manual source preference: {source_preference}
Conversation context: {context}
"""
