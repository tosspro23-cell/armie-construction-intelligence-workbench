"""SPEC-M16 SS A: tool schema definitions for the V2 tool-calling agent.

Each tool mirrors an existing, already-deterministic operation this system
supports today (``QueryPlan``/``IfcQueryInput``/``DocumentQueryInput``,
``apps/api/app/schemas/models.py``) -- no new computation logic exists here.
A tool call's parsed arguments build the *same* ``QueryPlan`` shape V1
already validates and executes, so ``capability_gate``,
``verify_execution_consistency``, and evidence construction are reused
unchanged by V2, not reimplemented.

Deliberately excluded (SPEC-M16 Explicitly excluded scope): no tool writes
anything back to the IFC model or an external system -- every tool here is
read-only, matching every capability this system has today.
"""

from __future__ import annotations

from typing import Any

from app.schemas.models import QueryPlan

# JSON-schema tool definitions, OpenAI/Azure OpenAI Chat Completions
# `tools=[...]` shape. Kept as plain dicts (not a new Pydantic layer) since
# this *is* the wire format the provider call needs verbatim.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "count_elements",
            "description": "Count how many IFC elements of a given type exist, optionally filtered to one storey.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "The canonical IFC entity type, e.g. IfcDoor, IfcWindow, IfcSpace."},
                    "storey": {"type": "string", "description": "Optional storey/floor name to filter to."},
                },
                "required": ["entity_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "group_elements_by_storey",
            "description": "Get a per-storey count breakdown for one IFC entity type, optionally identifying the storey with the most or fewest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "The canonical IFC entity type, e.g. IfcDoor, IfcWindow."},
                    "postprocess": {"type": "string", "enum": ["argmax", "argmin", "none"], "description": "argmax for 'which storey has the most', argmin for 'the fewest', none for the full breakdown."},
                },
                "required": ["entity_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_element_properties",
            "description": "Look up real IFC properties for elements of a given type (material, fire rating, load-bearing, dimensions, etc). If more than one element matches, every match's properties are returned -- this tool never guesses a single 'the' element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "The canonical IFC entity type, e.g. IfcDoor, IfcWindow."},
                    "global_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional: specific element GlobalIds to narrow to (e.g. a viewer selection). GlobalId is the IFC model's own internal identifier (looks like '0ehNcYPbH3JQicvZQLHP24') -- it is NOT the human-readable Tag/Mark used on a PDF schedule or a finding's own tag. To look up one specific element by that identifier, use `tags` instead."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: specific elements' own Tag/Mark value to narrow to -- this is the identifier a PDF schedule row or a finding's own tag actually uses (e.g. '2543664' or 'W02'), and the reliable way to look up one specific element by it. Prefer this over trying to guess a GlobalId, and over an unfiltered entity_type query for a large building (an unfiltered query only returns a sample, which may not include the specific element you need)."},
                },
                "required": ["entity_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_quantity",
            "description": "Get the maximum or minimum verified measurement (height, width, length, or area) across all elements of one IFC entity type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "The canonical IFC entity type, e.g. IfcDoor, IfcWindow, IfcWall."},
                    "measure": {"type": "string", "enum": ["height", "width", "length", "area"]},
                    "aggregation": {"type": "string", "enum": ["min", "max"]},
                },
                "required": ["entity_type", "measure", "aggregation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "space_distance",
            "description": "Get the bounded horizontal centroid distance between two named IFC spaces/rooms. A straight-line measurement, not a walkable route.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_space": {"type": "string"},
                    "to_space": {"type": "string"},
                },
                "required": ["from_space", "to_space"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_pdf_field",
            "description": "Extract one specific field's value (e.g. a connected load, a schedule entry) from the project's configured PDF drawing/schedule documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "The column/field name only, exactly as it appears as a table header, e.g. 'Connected Load', 'Height', 'Width'. Never include a record identifier here (a board, tag, or mark such as 'Panel-A' or 'W02') -- that goes only in `question`, since matching is done separately for the field/column and for the record/row.",
                    },
                    "question": {"type": "string", "description": "The original user question, verbatim, for evidence localization."},
                },
                "required": ["field", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_current_view",
            "description": "Inspect what is currently visible in the 3D viewer (only usable if the user has an active viewer snapshot/selection).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reconcile_doors_windows",
            "description": "Cross-check the IFC model's doors and windows against the PDF drawing/schedule for width/height consistency. Only covers door/window width and height -- never use this for any other attribute (e.g. fire rating, material), which this tool does not check.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Independent-review finding, 2026-09-19 (D-064 item 2): SPEC-M17's
# investigation feature originally read its final conclusion out of the
# model's own free-text answer (`AgentService._extract_proposed_dimension`)
# -- a real, confirmed fabrication risk (an unrelated tool result number
# could be adopted as a proposed dimension) and, separately, a shape that
# cannot express what a missing_in_pdf/missing_in_ifc investigation
# actually concludes ("is this a real omission, or did you find it under a
# different tag" is not a width or a height). This tool forces the model
# to submit its conclusion as typed, code-validated arguments instead --
# not offered on every V2 turn (see `include_verdict_tool` in
# `AgentService.invoke_v2`), only during a finding investigation, appended
# to `TOOL_DEFINITIONS` rather than living inside that list permanently.
SUBMIT_FINDING_VERDICT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_finding_verdict",
        "description": (
            "Submit your final, structured conclusion for this finding investigation. Call this exactly "
            "once, as your last action, after you have gathered enough evidence from your other tool calls. "
            "This structured submission -- not your free-text answer -- is what determines the proposed "
            "resolution a human will review, so it must reflect your actual, evidence-backed conclusion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["dimension_confirmed", "genuine_omission", "found_under_different_reference", "inconclusive"],
                    "description": (
                        "'dimension_confirmed': for a dimension-mismatch finding, you determined which side's "
                        "value is correct -- also fill in confirmed_width_m/confirmed_height_m. "
                        "'genuine_omission': for a missing-from-one-side finding, the element really is absent "
                        "from the source you checked, after a genuine multi-step effort to find it. "
                        "'found_under_different_reference': you found this element on the other side, under a "
                        "different tag/mark than the one under investigation -- describe what you found in "
                        "`basis`. 'inconclusive': you could not determine any of the above with confidence "
                        "after actually trying more than one approach."
                    ),
                },
                "confirmed_width_m": {"type": "number", "description": "Only with verdict='dimension_confirmed', and only if the mismatch concerns width: the width in meters you determined to be correct."},
                "confirmed_height_m": {"type": "number", "description": "Only with verdict='dimension_confirmed', and only if the mismatch concerns height: the height in meters you determined to be correct."},
                "basis": {"type": "string", "description": "One or two sentences naming the specific tool result(s) that support this verdict."},
            },
            "required": ["verdict", "basis"],
        },
    },
}


def build_plan_from_tool_call(tool_name: str, arguments: dict[str, Any]) -> QueryPlan:
    """Convert one model-requested tool call into the canonical QueryPlan
    shape V1's own execution/verification/evidence layer already consumes
    unchanged -- see this module's own docstring for why no new execution
    logic exists here.
    """
    common = dict(rationale=f"V2 tool call: {tool_name}", planning_mode="tool_calling", rule_id=f"tool:{tool_name}", match_status="complete")
    if tool_name == "count_elements":
        filters = {"storey": arguments["storey"]} if arguments.get("storey") else {}
        return QueryPlan(source="ifc", intent="count", operation="count", entity_type=arguments["entity_type"], filters=filters, group_by="none", expected_result_shape="scalar_count", **common)
    if tool_name == "group_elements_by_storey":
        postprocess = arguments.get("postprocess") or "none"
        return QueryPlan(
            source="ifc", intent="aggregate", operation="group_by", entity_type=arguments["entity_type"], filters={}, group_by="storey",
            postprocess=postprocess if postprocess != "none" else None,
            expected_result_shape="single_group_extremum" if postprocess in {"argmax", "argmin"} else "grouped_counts", **common,
        )
    if tool_name == "get_element_properties":
        filters = {}
        if arguments.get("global_ids"):
            filters["global_ids"] = arguments["global_ids"]
        if arguments.get("tags"):
            filters["tags"] = arguments["tags"]
        return QueryPlan(source="ifc", intent="property_lookup", operation="get_properties", entity_type=arguments["entity_type"], filters=filters, group_by="none", expected_result_shape="properties", **common)
    if tool_name == "aggregate_quantity":
        return QueryPlan(
            source="ifc", intent="aggregate", operation="aggregate_quantity", entity_type=arguments["entity_type"], filters={}, group_by="none",
            measure=arguments["measure"], aggregation=arguments["aggregation"], expected_result_shape="scalar_measurement", **common,
        )
    if tool_name == "space_distance":
        return QueryPlan(
            source="ifc", intent="relationship_lookup", operation="space_distance", entity_type="IfcSpace",
            filters={"from_space": arguments["from_space"], "to_space": arguments["to_space"]}, expected_result_shape="scalar_measurement", **common,
        )
    if tool_name == "extract_pdf_field":
        return QueryPlan(source="pdf", intent="document_lookup", operation="extract_field", requested_field=arguments["field"], filters={}, expected_result_shape="document_value", **common)
    if tool_name == "inspect_current_view":
        return QueryPlan(source="viewer_snapshot", intent="visual_inspection", operation="inspect_view", filters={}, expected_result_shape="visual_claim", **common)
    if tool_name == "reconcile_doors_windows":
        # Reconciliation is a special case, not routed through this
        # function: its two subplans (one IFC, one PDF) are executed
        # together by a dedicated join (`_synthesize_reconciliation_
        # response`), never independently -- see `graph.py`'s own V2 tool
        # dispatch, which builds the two-subplan MultiQueryPlan directly
        # rather than calling this function for this one tool.
        raise ValueError("reconcile_doors_windows is handled specially by the V2 loop, not via build_plan_from_tool_call.")
    raise ValueError(f"Unknown tool: {tool_name}")
