from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, Enum):
    IFC = "ifc"
    PDF = "pdf"
    VIEWER = "viewer_snapshot"
    UNKNOWN = "unknown"


class Disposition(str, Enum):
    ANSWERED = "answered"
    PARTIALLY_ANSWERED = "partially_answered"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED = "unsupported"
    REFUSED = "refused"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ViewerContext(BaseModel):
    selected_global_ids: list[str] = Field(default_factory=list)
    selected_express_ids: list[int] = Field(default_factory=list)
    camera_pose: dict[str, Any] = Field(default_factory=dict)
    snapshot_id: str | None = None
    screenshot_base64: str | None = None
    visible_element_ids: list[str] = Field(default_factory=list)
    selected_entity_type: str | None = None
    selected_display_name: str | None = None
    selection_cleared: bool = False
    snapshot_cleared: bool = False
    target_global_id: str | None = None
    target_entity_type: str | None = None
    target_visible: bool = False


class ChatRequest(BaseModel):
    request_id: str | None = None
    thread_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)
    viewer_context: ViewerContext | None = None
    source_preference: Literal["auto", "ifc", "pdf", "viewer_snapshot"] = "auto"


class ClarificationResumeRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class ConversationContext(BaseModel):
    # Normalized, language-neutral state used by the semantic planner.  The
    # legacy singular fields remain for backwards-compatible selected-element
    # and viewer workflows, while the collections retain compound turns.
    active_targets: list[dict[str, Any]] = Field(default_factory=list)
    active_entity_types: list[str] = Field(default_factory=list)
    active_filters_by_entity: dict[str, dict[str, Any]] = Field(default_factory=dict)
    active_storey: str | None = None
    previous_query_set: list[dict[str, Any]] = Field(default_factory=list)
    previous_subplans: list[dict[str, Any]] = Field(default_factory=list)
    last_user_intent: str | None = None
    last_answered_subtasks: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_subtasks: list[dict[str, Any]] = Field(default_factory=list)
    selected_entity: dict[str, Any] | None = None
    conversation_turn_id: int = 0
    response_language: str | None = None
    active_source: SourceType = SourceType.UNKNOWN
    active_entity_type: str | None = None
    active_entity_ids: list[str] = Field(default_factory=list)
    active_filters: dict[str, Any] = Field(default_factory=dict)
    active_group_by: str | None = None
    previous_query_plan: dict[str, Any] | None = None
    previous_answer_summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    source_preference: Literal["auto", "ifc", "pdf", "viewer_snapshot"] = "auto"
    active_snapshot_id: str | None = None


class QueryPlan(BaseModel):
    subtask_id: str | None = None
    source: Literal["ifc", "pdf", "viewer_snapshot", "unsupported"]
    intent: Literal[
        "count",
        "list",
        "aggregate",
        "property_lookup",
        "relationship_lookup",
        "document_lookup",
        "visual_inspection",
        "clarification",
        "unsupported",
    ]
    operation: Literal[
        "count",
        "list",
        "min",
        "max",
        "sum",
        "average",
        "group_by",
        "get_properties",
        "inspect_relationship",
        "extract_field",
        "inspect_view",
        "aggregate_quantity",
        "max_window_height",
        "space_distance",
    ] | None = None
    entity_type: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: Literal["storey", "space", "type", "none"] | None = None
    # `group_by` is the only grouping representation. Execution modifiers are
    # deliberately not allowed inside the generic filter map.
    postprocess: Literal["argmax", "argmin", "none"] | None = None
    expected_result_shape: Literal["scalar_count", "grouped_counts", "single_group_extremum", "list", "properties", "element_identity", "element_storey", "element_properties", "document_value", "visual_claim", "scalar_measurement", "clarification"] | None = None
    requested_field: str | None = None
    rationale: str
    planning_mode: Literal["heuristic", "llm", "context"] = "heuristic"
    rule_id: str | None = None
    matched_signals: list[str] = Field(default_factory=list)
    match_status: Literal["complete", "partial", "unknown"] = "unknown"
    # This is retained only for model self-reporting. It is never displayed as
    # calibrated system confidence and heuristic plans leave it unset.
    model_self_reported_confidence: float | None = Field(default=None, ge=0, le=1)
    measure: str | None = None
    aggregation: Literal["min", "max"] | None = None


class MultiQueryPlan(BaseModel):
    """Language-neutral, typed decomposition of one conversational turn."""

    intent: Literal["single_query", "multi_query", "clarification", "unsupported"] = "single_query"
    response_language: str = "en"
    subplans: list[QueryPlan] = Field(min_length=1, max_length=8)
    rationale: str = ""
    raw_user_message: str | None = None
    normalized_request: str | None = None
    interpretation_confidence: Literal["high", "medium", "low"] | None = None
    corrections: list[dict[str, str]] = Field(default_factory=list)
    requires_clarification: bool = False


class SupportedQueryPlan(BaseModel):
    """Repair-only schema which prevents an acknowledged supported task from
    being serialized as `unsupported` by a smaller local planner."""

    source: Literal["ifc", "pdf", "viewer_snapshot"]
    intent: Literal["count", "list", "aggregate", "property_lookup", "relationship_lookup", "document_lookup", "visual_inspection"]
    operation: Literal["count", "list", "min", "max", "sum", "average", "group_by", "get_properties", "inspect_relationship", "aggregate_quantity", "extract_field", "inspect_view", "max_window_height", "space_distance"]
    entity_type: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: Literal["storey", "space", "type", "none"] | None = None
    postprocess: Literal["argmax", "argmin", "none"] | None = None
    expected_result_shape: Literal["scalar_count", "grouped_counts", "single_group_extremum", "list", "properties", "element_identity", "element_storey", "element_properties", "document_value", "visual_claim", "scalar_measurement", "clarification"] | None = None
    requested_field: str | None = None
    measure: str | None = None
    aggregation: Literal["min", "max"] | None = None
    rationale: str = ""


class SupportedMultiQueryPlan(BaseModel):
    response_language: str = "en"
    subplans: list[SupportedQueryPlan] = Field(min_length=1, max_length=8)


class ResponseLanguage(BaseModel):
    code: Literal["en", "zh-CN", "pt-PT", "fr", "es"]


class ConversationDelta(BaseModel):
    """Model-only resolution of an anaphoric follow-up against verified state."""

    preserve_active_storey: bool
    target_entity_type: str | None = None
    operation: Literal["count", "group_by", "none"] = "none"
    rationale: str = ""


class AnswerSynthesis(BaseModel):
    """Natural-language rendering of verified facts only; no new claims."""

    answer_markdown: str = Field(min_length=1, max_length=2400)


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_type: SourceType
    source_file: str
    summary: str
    locator: dict[str, Any]
    extracted_value: Any | None = None
    confidence: float | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Citation(BaseModel):
    evidence_id: str
    source_type: SourceType
    label: str
    locator: dict[str, Any]


class VerifierResult(BaseModel):
    verifier: str
    passed: bool
    confidence: float | None = None
    reason: str
    corrected_value: Any | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class VerificationStatus(BaseModel):
    status: Literal["passed", "failed", "not_applicable"]
    verifier_results: list[VerifierResult] = Field(default_factory=list)
    reason: str | None = None


class AgentResponse(BaseModel):
    thread_id: str
    trace_id: str
    disposition: Disposition
    answer_markdown: str
    citations: list[Citation] = Field(default_factory=list)
    verification: VerificationStatus
    follow_up_suggestions: list[str] = Field(default_factory=list)
    viewer_actions: list[dict[str, Any]] = Field(default_factory=list)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    context_update: dict[str, Any] = Field(default_factory=dict)


class IfcQueryInput(BaseModel):
    operation: Literal[
        "count", "list", "min", "max", "sum", "average", "group_by",
        "get_properties", "inspect_relationship", "aggregate_quantity", "max_window_height", "space_distance"
    ]
    entity_type: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    property_path: str | None = None
    measure: str | None = None
    aggregation: Literal["min", "max"] | None = None
    group_by: Literal["storey", "space", "type", "none"] = "none"
    limit: int = Field(default=50, ge=1, le=200)


class IfcQueryResult(BaseModel):
    operation: str
    value: Any
    unit: str | None = None
    matched_count: int
    evidence: list[Evidence]
    matched_global_ids: list[str] = Field(default_factory=list)
    normalized_query: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class DocumentQueryInput(BaseModel):
    field: str
    question: str
    page_hint: int | None = None
    entity_hint: str | None = None
    region_hint: list[float] | None = None


class DocumentQueryResult(BaseModel):
    value: str | float | int | None
    unit: str | None = None
    page: int
    bbox: list[float] | None
    extraction_method: Literal["native_text", "layout", "vision", "hybrid"]
    confidence: float
    evidence: list[Evidence]
    ambiguity: str | None = None


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str
    thread_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    step: str
    event_type: Literal[
        "context_resolved", "clarification_requested", "route_selected",
        "plan_created", "tool_called", "tool_completed", "evidence_created",
        "candidate_answer_created", "verification_completed", "retry",
        "finalized", "refused", "error", "model_called", "model_completed",
        "model_failed", "provider_fallback", "plan_validated",
        "capability_gate_passed", "capability_gate_rejected", "selection_grounded",
        "source_override_applied", "fast_path_coverage", "decomposed",
        "subplan_started", "subplan_completed", "synthesized", "context_updated",
        "normalize_utterance", "plan_canonicalization", "semantic_validation",
        "semantic_repair", "model_escalation", "batch_eligibility",
        "execution_consistency", "result_shape_verification", "context_precedence",
        "followup_delta", "grouped_execution", "postprocess_argmax",
        "disposition_resolution", "timeout", "cancelled", "progress"
    ]
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    configured_provider: str | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    planning_mode: str | None = None
    model_call_count: int = 0
    tool_call_count: int = 0
    retry_count: int = 0
    provider_fallback_reason: str | None = None
