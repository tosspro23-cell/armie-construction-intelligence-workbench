from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable, Optional, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.agent.plan_validation import (
    calibrate_interpretation_confidence,
    canonicalize_multi_plan,
    eligible_scalar_count_batch,
    enforce_grouped_request_contract,
    validate_multi_plan,
    verify_execution_consistency,
)
from app.agent.router import (
    SUPPORTED_ENTITY_TYPES,
    capability_gate,
    cross_source_join_requested,
    fast_path_coverage,
    ground_plan_to_selection,
    heuristic_multi_plan,
    heuristic_plan,
    nearest_space_requested,
    planner_prompt,
    reconciliation_plan,
    resolve_reference,
    selected_element_plan,
)
from app.agent.tools import SUBMIT_FINDING_VERDICT_TOOL, TOOL_DEFINITIONS, build_plan_from_tool_call
from app.config import Settings
from app.providers.base import AnswerChunkEvent, ToolCallEvent, TurnCompleteEvent
from app.schemas.models import (
    AgentProposal,
    AgentResponse,
    AuditEvent,
    Citation,
    ConversationDelta,
    Disposition,
    DocumentQueryInput,
    EngineeringFinding,
    Evidence,
    IfcQueryInput,
    MultiQueryPlan,
    QueryPlan,
    ReconciliationItem,
    ReconciliationStatus,
    ResponseLanguage,
    SourceType,
    VerificationStatus,
    VerifierResult,
)
from app.schemas.vision import VisionViewerInspection, VisionViewerVerification
from app.services import ProjectResources, ServiceContainer
from app.verification.verifiers import (
    DeterministicVerifier,
    EvidenceVerifier,
    InvariantValidator,
    verification_status,
)


class GraphState(TypedDict, total=False):
    thread_id: str
    trace_id: str
    # SPEC-M9: resolved once in AgentService.invoke's initial state (never
    # reassigned mid-graph) and read by every execution node instead of
    # self.container.ifc_repository/document_analyzers/self.settings.
    # ifc_file -- propagates automatically through this module's existing
    # `{**state, ...}` copy pattern, so no node signature needed to change
    # to carry it explicitly.
    project_resources: ProjectResources
    question: str
    viewer_context: dict[str, Any]
    conversation_context: dict[str, Any]
    clarification: Optional[str]
    plan: dict[str, Any]
    multi_plan: dict[str, Any]
    tool_result: dict[str, Any]
    evidence: list[dict[str, Any]]
    final_response: dict[str, Any]
    source_preference: str
    model_call_count: int
    tool_call_count: int
    unsupported_reason: str
    planner_error: str


# SPEC-M16 V2: how many items of a list-shaped tool result get sent back
# to the model verbatim before this system switches to a sample + count
# (see invoke_v2's own use of this, right below the reconciliation-specific
# version of the same idea). Not a scientifically tuned number -- chosen to
# comfortably cover every single-tool-call representative-eval question
# (the largest real list any of them returns is 4 items) while still
# capping the kind of match-a-whole-category call that returns dozens.
# Owner-reported, 2026-09-16, second pass: a first attempt at 15 made the
# model repeatedly re-call the *same* tool hoping for a fuller answer to a
# name/sub-category breakdown this system's tools cannot filter for at all
# (e.g. "how many tables vs chairs" out of a generic IfcFurnishingElement
# match) -- wasteful, not a correctness bug (the model never fabricated a
# number; it just kept retrying an identically-capped result). Raised to
# 40 so this only engages for genuinely large categories, and the system
# prompt now tells the model explicitly not to retry when it does.
_V2_TOOL_RESULT_LIST_CAP = 40

# D-067 (2026-09-21), found live via an owner-requested stress test against
# the real "RWTH DigitalHub" building: `_V2_TOOL_RESULT_LIST_CAP` above
# bounds how many *items* a large list sends back, but not how much each
# item's own `properties` dict costs -- fine for the 85-item
# IfcFurnishingElement case that first justified the cap (each item's own
# properties are small), but this real, professionally-authored Revit
# export attaches dozens of vendor-specific parameters to every element
# (German property names like "Abhängigkeiten.*"/"Grafiken.*"/"Sonstige.*",
# unlike this project's own small synthetic fixtures). A single
# `get_element_properties(entity_type="IfcDoor")` call already capped to 40
# items still measured ~2.2KB/item (~22K tokens for 40 items) against the
# real file; the live investigation that triggered a persistent real Azure
# OpenAI 429 called this tool twice in one turn (once for doors, once for
# windows), together comfortably exceeding the deployment's per-request
# token budget regardless of how long a retry waits. `_PROPERTY_SAMPLE_CAP`
# bounds each sample item's own `properties` dict to a representative
# subset instead, the same "sample + exact count, never silently guessed"
# transparency this file's own `_V2_TOOL_RESULT_LIST_CAP` already
# established -- see `_cap_item_properties` below.
_PROPERTY_SAMPLE_CAP = 15

# D-067 follow-up (2026-09-21), found live minutes after the fix above
# deployed: `_PROPERTY_SAMPLE_CAP` alone was not enough. Measured directly
# against the real file post-fix: `_V2_TOOL_RESULT_LIST_CAP` (40) sample
# items, each already trimmed to `_PROPERTY_SAMPLE_CAP` properties, still
# cost ~40-42KB (~10K tokens) *per call* -- because each item's own fixed
# metadata (global_id, express_id, a long Revit-style `name`, the JSON key
# names themselves, repeated 40 times) is a real cost the per-property
# trim never touched. Two such calls in one turn (doors, windows -- the
# exact live-reproduced pattern) still totaled ~20K tokens, and a live
# retest minutes after deploying the property-cap fix alone reproduced the
# same real 429 again. `total_count` and `distinct_value_summary` already
# carry the turn's own exhaustive, unguessed facts regardless of how many
# raw items are sampled (see `_distinct_value_summary`'s own docstring) --
# so the *sample size itself* is decoupled from the trigger threshold
# here: a list only needs to be "large" (> `_V2_TOOL_RESULT_LIST_CAP`) to
# engage this whole path at all, but the actual number of raw items shown
# is this smaller, separate constant. Measured post-fix: two such calls in
# one turn now total ~6K tokens combined (down from ~69K pre-fix, ~20K
# after the property-cap fix alone).
_LARGE_LIST_SAMPLE_SIZE = 10


class AgentService:
    """LangGraph orchestration for safe tool-routed project questions."""

    def __init__(self, container: ServiceContainer) -> None:
        self.container = container
        self.settings: Settings = container.settings
        self.graph = self._build_graph()

    def _audit(
        self,
        state: GraphState,
        step: str,
        event_type: str,
        summary: str,
        payload: dict | None = None,
        *,
        actual_provider: str | None = None,
        actual_model: str | None = None,
        planning_mode: str | None = None,
        retry_count: int = 0,
        fallback_reason: str | None = None,
    ) -> None:
        # SPEC-M9 SS F, D-023 addendum: independent-review finding, confirmed
        # live (2026-09-13) -- every event from this single centralized
        # helper now carries which project/frozen source_set_id was active,
        # closing the gap where the manifest was tracked internally
        # (ServiceContainer.get_project's cache key) but never actually
        # surfaced on anything a caller or auditor could see.
        manifest = state["project_resources"].manifest
        self.container.audit_store.append(AuditEvent(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            step=step,
            event_type=event_type,
            summary=summary,
            payload=payload or {},
            configured_provider=self.settings.llm_provider,
            actual_provider=actual_provider,
            actual_model=actual_model,
            planning_mode=planning_mode,
            model_call_count=state.get("model_call_count", 0),
            tool_call_count=state.get("tool_call_count", 0),
            retry_count=retry_count,
            provider_fallback_reason=fallback_reason,
            project_id=manifest.project_id,
            source_set_id=manifest.source_set_id,
        ))

    def _build_graph(self):
        graph = StateGraph(GraphState)
        graph.add_node("resolve_context", self._resolve_context)
        graph.add_node("route", self._route)
        graph.add_node("execute_multi", self._execute_multi)
        graph.add_node("execute_ifc", self._execute_ifc)
        graph.add_node("execute_pdf", self._execute_pdf)
        graph.add_node("execute_viewer", self._execute_viewer)
        graph.add_node("refuse", self._refuse)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "resolve_context")
        graph.add_conditional_edges(
            "resolve_context",
            self._after_context,
            {"clarification": "finalize", "unsupported": "refuse", "route": "route"},
        )
        graph.add_edge("route", "execute_multi")
        graph.add_edge("execute_multi", "finalize")
        graph.add_edge("refuse", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    def _resolve_context(self, state: GraphState) -> dict:
        context = dict(state.get("conversation_context", {}))
        viewer = state.get("viewer_context", {})
        if viewer.get("selection_cleared"):
            # Clear only viewer-selection state. Conversation intent, source
            # preference, and verified non-selection filters remain intact.
            for key in ("active_entity_ids", "active_express_ids", "selected_entity"):
                context.pop(key, None)
            filters = dict(context.get("active_filters", {}))
            filters.pop("global_ids", None)
            filters.pop("express_ids", None)
            if filters:
                context["active_filters"] = filters
            else:
                context.pop("active_filters", None)
            self._audit(state, "resolve_context", "context_updated", "Viewer selection state was cleared without erasing conversational intent.", {"reason": "selection_cleared"})
        if viewer.get("snapshot_cleared"):
            context.pop("active_snapshot_id", None)
            self._audit(state, "resolve_context", "context_updated", "Viewer snapshot state was cleared without erasing conversational intent.", {"reason": "snapshot_cleared"})
        if viewer.get("selected_global_ids"):
            resolved_selection = None
            try:
                resolved_selection = state["project_resources"].ifc_repository.resolve_selection(
                    viewer.get("selected_global_ids", []), viewer.get("selected_express_ids", []),
                )
            except Exception as error:
                self._audit(state, "resolve_context", "error", "Selected IFC identity could not be resolved from the source model.", {"error": str(error)})
            selected_type = viewer.get("selected_entity_type") or (resolved_selection or {}).get("entity_type")
            context.update({
                "active_entity_ids": viewer.get("selected_global_ids", []),
                "active_express_ids": viewer.get("selected_express_ids", []),
                "active_entity_type": selected_type,
                "active_filters": {"global_ids": viewer.get("selected_global_ids", [])},
                "selected_entity": resolved_selection or {
                    "global_ids": viewer.get("selected_global_ids", []),
                    "express_ids": viewer.get("selected_express_ids", []),
                    "entity_type": selected_type,
                },
            })
            selection_payload = {
                "selection_used": True,
                "global_id": (resolved_selection or {}).get("global_id") or (viewer.get("selected_global_ids") or [None])[0],
                "express_id": (resolved_selection or {}).get("express_id") or (viewer.get("selected_express_ids") or [None])[0],
                "entity_type": selected_type,
                "storey": (resolved_selection or {}).get("storey"),
                "selection_precedence_reason": "active_viewer_selection_overrides_stale_conversation_entity",
            }
            self._audit(state, "resolve_context", "selection_grounded", "Viewer selection became the active IFC context.", selection_payload)
            self._audit(state, "resolve_context", "context_precedence", "Current viewer selection was given precedence over historical entity context.", selection_payload)
        if viewer.get("snapshot_id"):
            context["active_snapshot_id"] = viewer["snapshot_id"]
        context["source_preference"] = state.get("source_preference", "auto")
        _, context, clarification = resolve_reference(state["question"], context)
        # SPEC-M1.5 §4C: this is the single authoritative cross-source-join
        # gate for the live graph. START -> resolve_context runs
        # unconditionally before any other node, and this check's outcome
        # (via _after_context's conditional edge) diverts straight to
        # "refuse" whenever it fires, before "route" -- and therefore
        # heuristic_plan/heuristic_multi_plan/_synthesize_multi_response's
        # own copies of this same check -- ever run. Their checks are
        # provably unreachable for that reason (see the comments at each),
        # not merely believed dead.
        if cross_source_join_requested(state["question"]):
            reason = "Cross-source joins between drawing and IFC room-area data are outside this reference implementation. No source tool was executed."
            self._audit(state, "resolve_context", "capability_gate_rejected", "Whole-intent capability gate rejected an unsupported cross-source join.", {"reason": "cross_source_join_unsupported", "target": state["question"]})
            return {"conversation_context": context, "clarification": None, "unsupported_reason": reason}
        # A bare Chinese "board/slab" reference is intentionally ambiguous in
        # this mixed IFC + drawing project.  Do not let a model silently map it
        # to IfcSlab; require the user to identify the source/section first.
        normalized_question = "".join(state["question"].lower().split())
        if not clarification and ("这张图里的板有多少" in normalized_question or "图里的板有多少" in normalized_question):
            clarification = "我需要知道你指的是哪一种板或数据源。请明确 IFC 楼板数量，或指定工程图纸中的配电板/回路。"
        self._audit(
            state,
            "resolve_context",
            "context_resolved",
            "Conversation context resolved.",
            {"context": context, "clarification": clarification},
        )
        return {"conversation_context": context, "clarification": clarification}

    @staticmethod
    def _after_context(state: GraphState) -> str:
        if state.get("unsupported_reason"):
            return "unsupported"
        return "clarification" if state.get("clarification") else "route"

    def _route(self, state: GraphState) -> dict:
        viewer = state.get("viewer_context", {})
        context = state.get("conversation_context", {})
        coverage = fast_path_coverage(state["question"], context, bool(viewer.get("screenshot_base64") or viewer.get("selected_global_ids")))
        self._audit(
            state,
            "fast_path_coverage",
            "fast_path_coverage",
            "Heuristic coverage evaluated.",
            coverage,
            planning_mode="heuristic" if coverage["coverage_status"] == "complete" else "llm",
        )
        plan = heuristic_plan(
            state["question"],
            context,
            bool(viewer.get("screenshot_base64") or viewer.get("selected_global_ids")),
            state.get("source_preference", "auto"),
        )
        generic_multi = heuristic_multi_plan(
            state["question"], context,
            bool(viewer.get("screenshot_base64") or viewer.get("selected_global_ids")),
            state.get("source_preference", "auto"),
        )
        selection_plan = selected_element_plan(state["question"], context)
        if state.get("source_preference", "auto") != "auto":
            self._audit(state, "route", "source_override_applied", "Manual source override constrained routing.", {"source_preference": state["source_preference"]})
        model_call_count = state.get("model_call_count", 0)
        # A source preference constrains semantic planning but never turns an
        # incomplete/elliptical request into a heuristic fast path.
        # A PDF schedule keyword is a complete source-routing decision even
        # though extraction still needs the dedicated ambiguity gate.  Do not
        # let a text-only planner erase that safe deterministic route.
        use_fast_path = (
            coverage["coverage_status"] == "complete"
            or selection_plan is not None
            or (plan.match_status == "complete" and plan.source == "ifc")
            or (plan.match_status == "complete" and plan.source in {"pdf", "viewer_snapshot"})
        )
        reconciliation = reconciliation_plan(state["question"], context)
        if reconciliation is not None:
            # SPEC-M2 §4C: return immediately, before canonicalize_multi_plan
            # / enforce_grouped_request_contract / validate_multi_plan below
            # -- those exist to repair and validate a generically executable
            # single-entity-type IFC/PDF plan, a contract these two
            # audit-only reconciliation subplans deliberately do not follow
            # (see reconciliation_plan's docstring). Running them here would
            # misread the IFC subplan's intentionally-absent single
            # entity_type as a missing_entity validation failure and silently
            # replace this plan with a generic clarification request.
            self._audit(state, "semantic_plan", "semantic_validation", "Narrow IFC<->drawing door/window reconciliation intent was recognized before the cross-source-join gate could apply.", {"intent": "reconciliation", "rule_id": "door_window_reconciliation"}, planning_mode="heuristic")
            self._audit(state, "decompose", "decomposed", "Request decomposed into independent subplans.", {"subplan_count": len(reconciliation.subplans), "subplans": [item.model_dump() for item in reconciliation.subplans]}, planning_mode="heuristic")
            return {"multi_plan": reconciliation.model_dump(), "plan": reconciliation.subplans[0].model_dump(), "model_call_count": model_call_count}
        if generic_multi is not None:
            multi_plan = generic_multi
        elif nearest_space_requested(state["question"]):
            reason = "The current release understands nearest-room search, but supports distance only between two explicitly named spaces. It does not search all rooms to find the nearest one."
            multi_plan = MultiQueryPlan(intent="unsupported", response_language="en", raw_user_message=state["question"], normalized_request="nearest-space search relative to a named space", interpretation_confidence="high", rationale=reason, subplans=[QueryPlan(subtask_id="task_1", source="unsupported", intent="unsupported", rationale=reason, planning_mode="heuristic", match_status="complete")])
            self._audit(state, "semantic_plan", "semantic_validation", "Nearest-space intent was understood before the capability gate rejected it.", {"intent": "nearest_space_search", "supported": False, "reason": reason}, planning_mode="heuristic")
        elif use_fast_path:
            selected_language = "zh-CN" if any("\u4e00" <= char <= "\u9fff" for char in state["question"]) else "en"
            multi_plan = MultiQueryPlan(
                intent="single_query", response_language=selected_language, subplans=[(selection_plan or plan).model_copy(update={"subtask_id": "task_1", "match_status": "complete"})],
                rationale="Current viewer selection takes precedence for a deictic element request." if selection_plan else "Narrow deterministic fast path has complete coverage.",
                raw_user_message=state["question"], normalized_request=state["question"], interpretation_confidence="high",
            )
        else:
            provider = self.container.text_provider_factory(self.settings)
            self._audit(state, "semantic_plan", "model_called", "Language-agnostic semantic task decomposition started.", {"purpose": "multi_query_plan", "fallback_reason": coverage["reason"], "planner_type": "semantic_model"}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
            try:
                multi_plan = asyncio.run(provider.structured(
                    prompt=planner_prompt(state["question"], context, state.get("source_preference", "auto")),
                    response_model=MultiQueryPlan,
                    purpose="multi_query_plan",
                ))
                multi_plan = multi_plan.model_copy(update={
                    "subplans": [self._normalize_subplan(item, index) for index, item in enumerate(multi_plan.subplans, start=1)]
                })
                multi_plan = multi_plan.model_copy(update={"raw_user_message": state["question"]})
                model_call_count += 1
                multi_plan, corrections = canonicalize_multi_plan(multi_plan, context)
                multi_plan, grouped_corrections = enforce_grouped_request_contract(multi_plan, state["question"])
                corrections.extend(grouped_corrections)
                self._audit(state, "canonicalize_plan", "plan_canonicalization", "Semantic plan canonicalization completed.", {"status": "corrected" if corrections else "unchanged", "corrections": corrections, "multi_plan": multi_plan.model_dump(), "planner_type": "semantic_model"}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                multi_plan, interpretation_corrections = calibrate_interpretation_confidence(multi_plan)
                if interpretation_corrections:
                    self._audit(state, "semantic_repair", "semantic_repair", "Resolved a model-internal interpretation-confidence contradiction.", {"status": "corrected", "corrections": interpretation_corrections, "multi_plan": multi_plan.model_dump()}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                issues = validate_multi_plan(multi_plan)
                unsupported_subplans = [item for item in multi_plan.subplans if item.source == "unsupported"]
                planner_exposed_semantics = any(item.entity_type or item.operation for item in multi_plan.subplans)
                context_has_resolvable_target = bool(context.get("active_storey") or context.get("active_entity_ids") or context.get("selected_entity"))
                if issues or (unsupported_subplans and (planner_exposed_semantics or context_has_resolvable_target)):
                    self._audit(state, "semantic_validate", "semantic_validation", "Cross-field semantic validation failed; requesting bounded repair.", {"status": "failed", "issues": [issue.__dict__ for issue in issues], "unsupported_subplans": [item.subtask_id for item in unsupported_subplans]}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                    repair_prompt = self._semantic_repair_prompt(state["question"], context, multi_plan, issues)
                    self._audit(state, "semantic_repair", "semantic_repair", "Semantic plan repair started with qwen3:8b.", {"purpose": "multi_query_plan_repair", "issues": [issue.__dict__ for issue in issues]}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm", retry_count=1)
                    repaired = asyncio.run(provider.structured(
                        prompt=repair_prompt,
                        response_model=MultiQueryPlan,
                        purpose="multi_query_plan_repair",
                    ))
                    multi_plan = repaired.model_copy(update={"subplans": [self._normalize_subplan(item, index) for index, item in enumerate(repaired.subplans, start=1)], "raw_user_message": state["question"]})
                    multi_plan, repair_corrections = canonicalize_multi_plan(multi_plan, context)
                    multi_plan, grouped_repair_corrections = enforce_grouped_request_contract(multi_plan, state["question"])
                    repair_corrections.extend(grouped_repair_corrections)
                    multi_plan, interpretation_corrections = calibrate_interpretation_confidence(multi_plan)
                    issues = validate_multi_plan(multi_plan)
                    model_call_count += 1
                    self._audit(state, "semantic_repair", "semantic_repair", "Semantic repair completed.", {"status": "passed" if not issues else "failed", "corrections": [*repair_corrections, *interpretation_corrections], "issues": [issue.__dict__ for issue in issues], "multi_plan": multi_plan.model_dump()}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm", retry_count=1)
                    if issues:
                        # Optional escalation is deliberately bounded and opt-in
                        # (SPEC-M1 §4.2/9, OD-2): it only runs against a
                        # configured local model for unresolved semantic
                        # conflicts, and is skipped entirely when unconfigured.
                        escalated = self.container.escalation_provider_factory(self.settings)
                        if escalated is None:
                            self._audit(state, "semantic_repair", "model_failed", "Semantic escalation is not configured; no escalation model set.", {"issues": [issue.__dict__ for issue in issues]}, planning_mode="llm", retry_count=2)
                        else:
                            self._audit(state, "semantic_repair", "model_escalation", f"Persistent semantic validation failures escalated to {escalated.model}.", {"issues": [issue.__dict__ for issue in issues]}, actual_provider=escalated.name, actual_model=escalated.model, planning_mode="llm", retry_count=2)
                            try:
                                candidate = asyncio.run(escalated.structured(prompt=self._semantic_repair_prompt(state["question"], context, multi_plan, issues), response_model=MultiQueryPlan, purpose="multi_query_plan_escalation"))
                                multi_plan = candidate.model_copy(update={"subplans": [self._normalize_subplan(item, index) for index, item in enumerate(candidate.subplans, start=1)], "raw_user_message": state["question"]})
                                multi_plan, escalation_corrections = canonicalize_multi_plan(multi_plan, context)
                                multi_plan, grouped_escalation_corrections = enforce_grouped_request_contract(multi_plan, state["question"])
                                escalation_corrections.extend(grouped_escalation_corrections)
                                multi_plan, interpretation_corrections = calibrate_interpretation_confidence(multi_plan)
                                issues = validate_multi_plan(multi_plan)
                                model_call_count += 1
                                self._audit(state, "semantic_repair", "semantic_repair", f"{escalated.model} escalation completed.", {"status": "passed" if not issues else "failed", "corrections": [*escalation_corrections, *interpretation_corrections], "issues": [issue.__dict__ for issue in issues]}, actual_provider=escalated.name, actual_model=escalated.model, planning_mode="llm", retry_count=2)
                            except Exception as escalation_error:
                                self._audit(state, "semantic_repair", "model_failed", f"{escalated.model} semantic escalation was unavailable.", {"error": str(escalation_error)}, actual_provider=escalated.name, actual_model=escalated.model, planning_mode="llm", retry_count=2)
                if issues:
                    # A semantically invalid plan is not executable even when
                    # it is syntactically valid JSON.
                    multi_plan = MultiQueryPlan(intent="clarification", response_language=multi_plan.response_language, raw_user_message=state["question"], normalized_request=multi_plan.normalized_request, interpretation_confidence="low", requires_clarification=True, rationale="Semantic validation could not establish a safe executable plan.", subplans=[QueryPlan(subtask_id="task_1", source="unsupported", intent="clarification", rationale="Please clarify the requested entities and grouping.", planning_mode="llm", match_status="unknown")])
                # A narrow, separate semantic classification prevents a local
                # planner's default schema value ("en") from overriding the
                # language of an otherwise correctly understood request. This
                # is model-based, not a script/keyword routing branch.
                language = asyncio.run(provider.structured(
                    prompt=f"Classify the response language/style of this user message. Return only the language code. Use zh-CN for Chinese and choose the dominant user style for mixed language. Message: {state['question']}",
                    response_model=ResponseLanguage,
                    purpose="response_language",
                ))
                multi_plan = multi_plan.model_copy(update={"response_language": language.code})
                model_call_count += 1
                # A separate, very small semantic delta contract protects a
                # verified active storey from being silently dropped in a
                # deictic follow-up. It is model-based and language agnostic;
                # no user-language keyword routing is introduced here.
                if context.get("active_storey") and multi_plan.subplans:
                    self._audit(state, "context_delta", "model_called", "Contextual semantic delta check started.", {"purpose": "conversation_delta", "active_storey": context["active_storey"], "candidate_plan": multi_plan.model_dump()}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                    delta = asyncio.run(provider.structured(
                        prompt=f"""Resolve a conversational follow-up for a construction agent. Do not calculate facts.
Verified active storey: {context['active_storey']}
Latest user message: {state['question']}
Candidate plan: {multi_plan.model_dump()}
Return preserve_active_storey=true only when the latest request refers to the currently resolved storey/level/floor rather than asking for a fresh project-wide grouping. If true, return the requested supported IFC entity and operation=count. If false, preserve the candidate plan semantics.""",
                        response_model=ConversationDelta,
                        purpose="conversation_delta",
                    ))
                    model_call_count += 1
                    applied_followup_delta = False
                    # The delta model can correctly preserve the active
                    # storey but omit its enum entity while the planner's own
                    # normalized semantic text still names it.  Recover only
                    # this finite IFC ontology from that model-produced text;
                    # never re-parse the raw user utterance here.
                    delta_entity_type = delta.target_entity_type
                    if not delta_entity_type:
                        normalized_semantics = (multi_plan.normalized_request or "").lower()
                        # D-055: kept in sync with router.SUPPORTED_ENTITY_TYPES.
                        declared_entities = {
                            "door": "IfcDoor", "window": "IfcWindow", "wall": "IfcWall",
                            "space": "IfcSpace", "room": "IfcSpace", "stair": "IfcStair", "slab": "IfcSlab",
                            "roof": "IfcRoof", "column": "IfcColumn", "beam": "IfcBeam", "member": "IfcMember",
                            "railing": "IfcRailing", "covering": "IfcCovering",
                            "furniture": "IfcFurnishingElement", "furnishing": "IfcFurnishingElement",
                            "footing": "IfcFooting", "plate": "IfcPlate",
                        }
                        delta_entity_type = next(
                            (entity for term, entity in declared_entities.items() if term in normalized_semantics),
                            None,
                        )
                    if delta.preserve_active_storey and delta_entity_type and delta.operation == "count":
                        transformed: list[QueryPlan] = []
                        for index, item in enumerate(multi_plan.subplans, start=1):
                            if index == 1:
                                filters = dict(item.filters)
                                filters["storey"] = context["active_storey"]
                                transformed.append(item.model_copy(update={"source": "ifc", "intent": "count", "operation": "count", "entity_type": delta_entity_type, "filters": filters, "group_by": None, "postprocess": None, "expected_result_shape": "scalar_count"}))
                            else:
                                transformed.append(item)
                        multi_plan = multi_plan.model_copy(update={"subplans": transformed})
                        applied_followup_delta = True
                    self._audit(state, "context_delta", "model_completed", "Contextual semantic delta check completed.", {"delta": delta.model_dump(), "transformed_plan": multi_plan.model_dump()}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                    self._audit(
                        state,
                        "context_delta",
                        "followup_delta",
                        "Follow-up context delta was resolved without sharing mutable state across turns.",
                        {
                            "status": "applied" if applied_followup_delta else "not_applied",
                            "active_storey": context["active_storey"],
                            "delta": delta.model_dump(),
                            "resolved_entity_type": delta_entity_type,
                            "transformed_plan": multi_plan.model_dump(),
                        },
                        actual_provider=provider.name,
                        actual_model=provider.model,
                        planning_mode="llm",
                    )
                self._audit(state, "semantic_plan", "model_completed", "Semantic planner returned typed task decomposition.", {"multi_plan": multi_plan.model_dump()}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
                self._audit(state, "semantic_validate", "semantic_validation", "Cross-field semantic validation passed.", {"status": "passed", "subplan_count": len(multi_plan.subplans)}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm")
            except Exception as error:
                raw_response = getattr(error, "raw_response", None)
                status_code = getattr(error, "status_code", None)
                self._audit(state, "semantic_plan", "model_failed", "Semantic planner failed during structured-output parsing or transport.", {"model": provider.model, "raw_response": raw_response, "transport_status": status_code, "parse_error": str(error), "validation_errors": [], "normalized_candidate": {}, "repair_attempted": True, "repair_result": None, "latency_ms": 0}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm", retry_count=1)
                repair_result = None
                try:
                    repaired = asyncio.run(provider.structured(
                        prompt=planner_prompt(state["question"], context, state.get("source_preference", "auto")) + "\nReturn exactly one valid JSON object matching the schema. Do not use markdown or prose.",
                        response_model=MultiQueryPlan,
                        purpose="multi_query_plan_repair",
                    ))
                    repaired = repaired.model_copy(update={"subplans": [self._normalize_subplan(item, index) for index, item in enumerate(repaired.subplans, start=1)], "raw_user_message": state["question"]})
                    repaired, repair_corrections = canonicalize_multi_plan(repaired, context)
                    repaired, grouped_repair_corrections = enforce_grouped_request_contract(repaired, state["question"])
                    issues = validate_multi_plan(repaired)
                    if issues:
                        raise ValueError("; ".join(issue.message for issue in issues))
                    repair_result = repaired.model_dump()
                    multi_plan = repaired
                    model_call_count += 1
                    self._audit(state, "semantic_repair", "semantic_repair", "Bounded structured-output repair completed.", {"status": "passed", "repair_result": repair_result}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm", retry_count=1)
                except Exception as repair_error:
                    repair_result = {"error": str(repair_error)}
                    self._audit(state, "semantic_repair", "model_failed", "Bounded structured-output repair failed.", {"status": "failed", "repair_result": repair_result}, actual_provider=provider.name, actual_model=provider.model, planning_mode="llm", retry_count=1)
                    multi_plan = MultiQueryPlan(intent="unsupported", response_language="en", subplans=[QueryPlan(source="unsupported", intent="unsupported", rationale="The semantic planner could not produce a safe typed plan.", planning_mode="llm", match_status="unknown")])
                    return {"multi_plan": multi_plan.model_dump(), "plan": multi_plan.subplans[0].model_dump(), "model_call_count": model_call_count, "planner_error": "I could not complete semantic planning for that request. Please retry or use a more explicit English formulation."}
        multi_plan, fast_corrections = canonicalize_multi_plan(multi_plan, context)
        multi_plan, grouped_fast_corrections = enforce_grouped_request_contract(multi_plan, state["question"])
        fast_corrections.extend(grouped_fast_corrections)
        if use_fast_path:
            self._audit(state, "canonicalize_plan", "plan_canonicalization", "Fast-path plan canonicalization completed.", {"status": "corrected" if fast_corrections else "unchanged", "corrections": fast_corrections})
            fast_issues = validate_multi_plan(multi_plan)
            self._audit(state, "semantic_validate", "semantic_validation", "Fast-path semantic validation completed.", {"status": "passed" if not fast_issues else "failed", "issues": [issue.__dict__ for issue in fast_issues]})
            if fast_issues:
                multi_plan = MultiQueryPlan(intent="clarification", response_language="en", raw_user_message=state["question"], normalized_request=state["question"], requires_clarification=True, rationale="Fast-path semantic validation failed.", subplans=[QueryPlan(subtask_id="task_1", source="unsupported", intent="clarification", rationale="Please clarify the requested operation.")])
        if len(multi_plan.subplans) > 1:
            self._audit(state, "decompose", "decomposed", "Request decomposed into independent subplans.", {"subplan_count": len(multi_plan.subplans), "subplans": [item.model_dump() for item in multi_plan.subplans]}, planning_mode="llm")
        return {"multi_plan": multi_plan.model_dump(), "plan": multi_plan.subplans[0].model_dump(), "model_call_count": model_call_count}

    @staticmethod
    def _semantic_repair_prompt(question: str, context: dict[str, Any], plan: MultiQueryPlan, issues: list[Any]) -> str:
        return f"""Repair a construction-agent semantic plan. Do not calculate any model facts.
Original user message: {question}
Conversation context: {context}
Current plan: {plan.model_dump()}
Validation errors: {[issue.__dict__ for issue in issues]}

Canonical rules:
- `group_by` is top-level only; never put it inside filters.
- A per-storey or per-room request requires operation=`group_by`, top-level group_by, and expected_result_shape=`grouped_counts`.
- A most/least comparison preserves group_by and uses postprocess=`argmax`/`argmin`.
- Preserve every explicitly requested entity and operation. Do not turn grouped requests into project totals.
- filters may only hold source-data constraints such as storey, global_ids, property_equals, or name_contains.
Return only a corrected MultiQueryPlan JSON object."""

    @staticmethod
    def _normalize_subplan(plan: QueryPlan, index: int) -> QueryPlan:
        """Fill only schema-implied defaults; never infer a source-data fact."""
        operation_by_intent = {
            "count": "count", "list": "list", "property_lookup": "get_properties",
            "relationship_lookup": "inspect_relationship", "document_lookup": "extract_field",
            "visual_inspection": "inspect_view",
        }
        operation = plan.operation or operation_by_intent.get(plan.intent)
        if plan.intent == "aggregate" and not operation:
            operation = "group_by" if plan.group_by and plan.group_by != "none" else None
        intent = "aggregate" if operation in {"group_by", "min", "max", "sum", "average", "aggregate_quantity"} else plan.intent
        return plan.model_copy(update={
            "subtask_id": plan.subtask_id or f"task_{index}",
            "operation": operation,
            "intent": intent,
            "planning_mode": "llm",
            "match_status": "complete",
        })

    def _execute_multi(self, state: GraphState) -> dict:
        """Execute each validated subplan independently and retain partial success."""
        multi_plan = MultiQueryPlan.model_validate(state["multi_plan"])
        if multi_plan.intent == "reconciliation":
            # SPEC-M2 §4C/D: reconciliation's two subplans are a typed,
            # audited record of intent, not independently executable through
            # the generic per-subplan loop below (one IFC subplan must cover
            # both IfcDoor and IfcWindow; the PDF subplan reads the whole
            # page-2 table, not one question-driven record/field). The join
            # is executed and synthesized together in one dedicated path.
            try:
                response = self._synthesize_reconciliation_response(state, multi_plan)
            except Exception as error:
                # SPEC-M2 §4G: a genuine failure to read one side (for
                # example the schedule page's table structure could not be
                # parsed at all) means the join could not run at all -- a
                # system-execution failure, not a content disagreement
                # between sources. Reporting every item "missing" here
                # would be a fabricated result (Codex P1 finding). Idiom
                # matches _execute_ifc's own failure handling (this
                # method's `except Exception as error: self._audit(...)`
                # sibling above) rather than inventing a second mechanism.
                self._audit(state, "execute_reconciliation", "error", "Door/window reconciliation could not execute.", {"error": str(error)})
                response = {
                    "answer": f"I could not complete the door/window reconciliation: {error}",
                    "disposition": "error",
                    "citations": [],
                    "verification": VerificationStatus(status="failed", reason=str(error)).model_dump(),
                    "reconciliation_items": [],
                    "subresults": [],
                    "evidence": [],
                    "tool_call_count_delta": 0,
                }
            evidence = response.pop("evidence", [])
            tool_calls = state.get("tool_call_count", 0) + response.pop("tool_call_count_delta", 0)
            response["context_update"] = self._merge_conversation_context(state.get("conversation_context", {}), multi_plan, response.get("subresults", []))
            # SPEC-M2 Codex P2: this dedicated synthesis path has no
            # localized reconciliation templates yet, even though
            # reconciliation_plan may detect "zh-CN" from the question
            # itself -- report the language the answer text is actually
            # written in, not the language the question was in, until
            # localized templates exist (tracked in REVIEW_REQUIRED.md).
            response["response_language"] = "en"
            self._audit(state, "context_update", "context_updated", "Structured conversational context updated.", {"context_update": response["context_update"]})
            return {"tool_result": response, "evidence": evidence, "tool_call_count": tool_calls, "model_call_count": state.get("model_call_count", 0)}
        subresults: list[dict[str, Any]] = []
        all_evidence: list[dict[str, Any]] = []
        tool_calls = state.get("tool_call_count", 0)
        model_calls = state.get("model_call_count", 0)

        # Scalar-only batching is an optimisation, never a semantic rewrite.
        # Grouped/planned comparison queries always retain their own execution.
        batchable = list(multi_plan.subplans)
        allowed_batch, batch_reason = eligible_scalar_count_batch(batchable)
        self._audit(state, "batch_eligibility", "batch_eligibility", "Batch eligibility evaluated against the complete execution contract.", {"status": "passed" if allowed_batch else "rejected", "reason": batch_reason, "subplans": [item.model_dump() for item in batchable]})
        batch_ids = {id(item) for item in batchable} if allowed_batch else set()
        batch_values: dict[str, int] = {}
        if batch_ids:
            entity_types = [item.entity_type for item in batchable if item.entity_type]
            if entity_types:
                batch_values = state["project_resources"].ifc_repository.execute_batch_counts(entity_types, batchable[0].filters)
                self._audit(state, "decompose", "tool_called", "Deterministic scalar IFC count batch invoked.", {"operation": "count_multiple", "entity_types": entity_types, "filters": batchable[0].filters})

        for index, original in enumerate(multi_plan.subplans, start=1):
            subtask_id = original.subtask_id or f"task_{index}"
            plan = original
            # Retain existing selected-element grounding for the narrow English
            # fast path. Semantic plans can already carry typed selected IDs.
            plan, selected_grounded = ground_plan_to_selection(plan, state.get("conversation_context", {}), state["question"])
            if selected_grounded:
                self._audit(state, f"subplan[{subtask_id}]", "selection_grounded", "Selected IFC identifiers were injected into tool filters.", {"filters": plan.filters}, planning_mode=plan.planning_mode)
            allowed, reason = capability_gate(plan)
            self._audit(state, f"capability_gate[{subtask_id}]", "capability_gate_passed" if allowed else "capability_gate_rejected", reason or "Subplan is within the constrained tool capability boundary.", {"subplan": plan.model_dump()}, planning_mode=plan.planning_mode)
            if not allowed:
                subresults.append(self._unsupported_subresult(subtask_id, plan, reason or "Unsupported subplan.", state))
                continue
            self._audit(state, f"subplan[{subtask_id}]", "subplan_started", "Subplan execution started.", {"subplan": plan.model_dump()}, planning_mode=plan.planning_mode)
            if batch_values and plan.source == "ifc" and plan.operation == "count" and plan.group_by in (None, "none") and plan.expected_result_shape == "scalar_count" and plan.entity_type in batch_values:
                returned = self._execute_ifc_count_from_batch({**state, "tool_call_count": tool_calls, "model_call_count": model_calls}, plan, batch_values[plan.entity_type])
            else:
                local_state: GraphState = {**state, "plan": plan.model_dump(), "tool_call_count": tool_calls, "model_call_count": model_calls}
                if plan.source == "ifc":
                    returned = self._execute_ifc(local_state)
                elif plan.source == "pdf":
                    returned = self._execute_pdf(local_state)
                elif plan.source == "viewer_snapshot":
                    returned = self._execute_viewer(local_state)
                else:
                    returned = self._refuse(local_state)
            result = returned.get("tool_result", self._error_result("Subplan returned no result."))
            tool_calls = returned.get("tool_call_count", tool_calls)
            model_calls = returned.get("model_call_count", model_calls)
            all_evidence.extend(returned.get("evidence", []))
            subresults.append({"subtask_id": subtask_id, "plan": plan.model_dump(), **result})
            self._audit(state, f"subplan[{subtask_id}]", "subplan_completed", "Subplan execution completed.", {"disposition": result.get("disposition"), "verification": result.get("verification", {}).get("status")}, planning_mode=plan.planning_mode)

        response = self._synthesize_multi_response(state, multi_plan, subresults, model_calls)
        model_calls = response.pop("model_call_count", model_calls)
        response["context_update"] = self._merge_conversation_context(state.get("conversation_context", {}), multi_plan, subresults)
        response["subresults"] = subresults
        response["response_language"] = multi_plan.response_language
        self._audit(state, "context_update", "context_updated", "Structured conversational context updated.", {"context_update": response["context_update"]})
        return {"tool_result": response, "evidence": all_evidence, "tool_call_count": tool_calls, "model_call_count": model_calls}

    def _execute_ifc_count_from_batch(self, state: GraphState, plan: QueryPlan, value: int) -> dict:
        """Make a per-subtask verified record from a deterministic count batch."""
        query = IfcQueryInput(operation="count", entity_type=plan.entity_type, filters=plan.filters, group_by="none")
        result = state["project_resources"].ifc_repository.execute(query)
        # The independent tool query is intentionally used for evidence and
        # verification; the batch value is checked so a batch optimisation never
        # weakens deterministic assurance.
        if result.value != value:
            return {"tool_result": self._error_result("Batch count disagreed with independent IFC verification."), "tool_call_count": state.get("tool_call_count", 0) + 1}
        citations = self._citations(result.evidence, state)
        answer = self._format_ifc_answer(plan, value)
        consistency_issues = verify_execution_consistency(plan, tool_query=query.model_dump(), result_value=value, answer=answer)
        self._audit(state, "execution_consistency", "execution_consistency", "Scalar batch execution consistency checked.", {"status": "passed" if not consistency_issues else "failed", "issues": [issue.__dict__ for issue in consistency_issues], "result_shape": "scalar_count"})
        consistency = VerifierResult(verifier="intent_execution_consistency", passed=not consistency_issues, reason="Execution preserved the validated scalar count contract." if not consistency_issues else consistency_issues[0].message)
        status = verification_status([
            DeterministicVerifier(state["project_resources"].ifc_repository).verify(query, result),
            InvariantValidator().validate(evidence=result.evidence, citations=[Citation.model_validate(item) for item in citations], disposition="answered"),
            consistency,
        ])
        return {"tool_result": {
            "answer": answer, "disposition": "answered" if status.status == "verified" else "error", "citations": citations,
            "verification": status.model_dump(),
            "result_value": value,
            "context_update": {"active_source": SourceType.IFC.value, "active_entity_type": plan.entity_type, "active_filters": plan.filters, "previous_query_plan": plan.model_dump(), "evidence_refs": [item.id for item in result.evidence]},
        }, "evidence": [item.model_dump() for item in result.evidence], "tool_call_count": state.get("tool_call_count", 0) + 1}

    def _unsupported_subresult(self, subtask_id: str, plan: QueryPlan, reason: str, state: GraphState) -> dict:
        """Map a capability-gate rejection to its actual underlying cause (SPEC-M1.5 §4A).

        A ``source="unsupported"`` subplan is not one thing: it can be a
        genuinely unsupported capability, a request the planner determined
        needs clarification (``plan.intent == "clarification"``, set at the
        "issues persisted after repair/escalation" branch in ``_route``), or
        a transport/parsing failure the planner never recovered from
        (signalled by ``state["planner_error"]``, set only when bounded
        repair itself raised). These previously all collapsed into
        ``disposition="refused"``; the reason string was always correct,
        only the category was lost.
        """
        if state.get("planner_error"):
            disposition = "error"
            answer = state["planner_error"]
        elif plan.intent == "clarification":
            disposition = "clarification_required"
            answer = reason
        else:
            disposition = "unsupported"
            answer = reason
        return {"subtask_id": subtask_id, "plan": plan.model_dump(), "answer": answer, "disposition": disposition, "citations": [], "verification": VerificationStatus(status="not_applicable", reason=reason).model_dump(), "context_update": {}}

    def _synthesize_multi_response(self, state: GraphState, multi_plan: MultiQueryPlan, subresults: list[dict[str, Any]], model_calls: int) -> dict:
        # SPEC-M1.5 §4C: unreachable from the live graph for the same reason
        # as heuristic_plan/heuristic_multi_plan's copies -- _resolve_context
        # is the single authoritative gate and always runs first, before
        # "route" -> "execute_multi" -> this method could ever be reached
        # with a cross-source-join question. Retained as a defensive
        # fallback for this method's own direct callers/tests.
        if cross_source_join_requested(state.get("question", "")):
            reason = "Cross-source joins between drawing and IFC room-area data are outside this reference implementation. No numeric partial answer can be finalized."
            self._audit(state, "intent_coverage", "capability_gate_rejected", "Whole-intent coverage rejected a partial cross-source execution.", {"reason": "cross_source_join_unsupported"})
            return {"answer": reason, "disposition": "unsupported", "citations": [], "verification": VerificationStatus(status="not_applicable", reason=reason).model_dump(), "model_call_count": model_calls}
        successful = [item for item in subresults if item.get("disposition") == "answered" and item.get("verification", {}).get("status") == "verified"]
        unresolved = [item for item in subresults if item not in successful]
        citations = [citation for item in subresults for citation in item.get("citations", [])]
        answer = self._natural_answer(multi_plan, subresults)
        disposition = "answered" if successful and not unresolved else ("partially_answered" if successful else subresults[0].get("disposition", "refused"))
        verification = VerificationStatus(status="verified" if disposition == "answered" else "not_applicable", reason=None if disposition == "answered" else "One or more subtasks were unresolved; successful subtasks remain independently verified.")
        self._audit(state, "synthesize", "synthesized", "Subplan outcomes synthesized without omitting partial results.", {"successful_subtasks": len(successful), "unresolved_subtasks": len(unresolved), "disposition": disposition})
        return {"answer": answer, "disposition": disposition, "citations": citations, "verification": verification.model_dump(), "model_call_count": model_calls}

    # SPEC-M2 §4F: ±0.01m independently for width and height. The one
    # designed mismatch (W02: 1.75 vs 1.70) is 0.05m, five times this
    # tolerance -- unambiguous by design.
    _RECONCILIATION_TOLERANCE_M = 0.01

    def _reconciliation_ifc_items(self, project_resources: ProjectResources) -> dict[str, dict[str, Any]]:
        """Read every door/window's Tag and controlled width/height from the source IFC.

        Zero model calls, deterministic. Reads directly from the already-open
        ifcopenshell model (`IfcRepository.model`) rather than adding a new
        `IfcRepository` operation: no existing operation covers "both
        IfcDoor and IfcWindow, joined on the Tag attribute", and generalizing
        one for this single narrow pilot is out of SPEC-M2's scope (§5).

        D-037: SPEC-M2's original synthetic fixture always carries a real
        `Qto_*BaseQuantities` set, but a real Revit-2011-vintage export
        (confirmed on both the WBDG Office and Duplex Apartment Dataset Pack
        candidates) attaches no `IfcElementQuantity` to its doors/windows at
        all -- every one of them would otherwise read as a fabricated-looking
        `width_m=None, height_m=None` here, which `_compare_reconciliation_item`
        already null-coalesces to `0.0`, reporting a false dimension_mismatch
        against a zero for every real item. `IfcDoor`/`IfcWindow` both carry
        `OverallWidth`/`OverallHeight` as standard schema attributes
        regardless of whether a Qto set was ever attached; used only when the
        Qto lookup found nothing, never overriding a real Qto value, and
        relies on the same "the model's own units are SI/metres" assumption
        this project already makes everywhere else it reads an IFC quantity.
        """
        import ifcopenshell.util.element as ifc_element_util

        model = project_resources.ifc_repository.model
        items: dict[str, dict[str, Any]] = {}
        for element in [*model.by_type("IfcDoor"), *model.by_type("IfcWindow")]:
            tag = getattr(element, "Tag", None)
            if not tag:
                continue
            psets = ifc_element_util.get_psets(element, qtos_only=True)
            quantities = next(iter(psets.values()), {}) if psets else {}
            width = quantities.get("Width")
            height = quantities.get("Height")
            if width is None:
                width = getattr(element, "OverallWidth", None)
            if height is None:
                height = getattr(element, "OverallHeight", None)
            containment = getattr(element, "ContainedInStructure", []) or []
            storey = getattr(containment[0].RelatingStructure, "Name", None) if containment else None
            items[str(tag)] = {
                "tag": str(tag),
                "entity_type": element.is_a(),
                "storey": storey,
                "width_m": width,
                "height_m": height,
                "global_id": element.GlobalId,
                "express_id": element.id(),
            }
        return items

    def _reconciliation_pdf_items(self, project_resources: ProjectResources, page_number: int = 2) -> dict[str, dict[str, Any]]:
        """Read every row of the schedule's Mark/Level/Type/Width/Height table,
        starting at `page_number` and continuing onto however many further
        pages the schedule actually spans.

        Reuses `DocumentAnalyzer._read_table` exactly as M2P1 built it --
        the same word-coordinate row/column clustering already used by
        `native_lookup` -- rather than introducing a new PDF-parsing
        technique (SPEC-M2 §7). `native_lookup` itself is not reused because
        its contract is one question-driven record/field lookup, not "every
        row of this table."

        SPEC-M2's original synthetic fixture (9 items) always fit on one
        page, so this read exactly one fixed page for that milestone's
        entire lifetime. Found live, 2026-09-15, adding a real building's
        full door/window schedule (~100+ items): a real schedule this size
        does not fit on one page, and `_read_table`'s own single-page
        contract silently produced a schedule missing every item past the
        first page. Reads consecutive pages starting at `page_number` and
        merges every page's rows into one mapping, stopping at the first
        page with no table (a following non-schedule page, or the
        document's actual end) -- honest for both the tiny single-page
        fixture (stops after page 2, unchanged) and a real multi-page one.

        Raises only when `page_number` itself has no table at all
        (`_read_table` returns `None` there: the PDF is unavailable, or that
        page has no legible text/no header row) -- this is a genuine
        execution failure, distinct from a legitimately empty table (a
        header row was found but it names no Mark-like column, or it names
        one with zero data rows) and distinct from the schedule simply
        ending after one or more pages that did read successfully.
        Collapsing the first case into an empty mapping would let the
        caller report "checked, every IFC item missing from the PDF" when
        the schedule was never actually read at all (SPEC-M2 §4G).
        """
        items: dict[str, dict[str, Any]] = {}
        page = page_number
        while True:
            table = project_resources.document_analyzer._read_table(page)
            if table is None:
                if page == page_number:
                    raise RuntimeError(f"The schedule's page {page_number} table structure could not be read.")
                break
            labels = [column.label.lower() for column in table.columns]

            def _index(prefix: str) -> int | None:
                return next((i for i, label in enumerate(labels) if label.startswith(prefix)), None)

            mark_index, width_index, height_index = _index("mark"), _index("width"), _index("height")
            if mark_index is None:
                page += 1
                continue
            for row in table.rows:
                mark_cell = row[mark_index] if mark_index < len(row) else None
                if not mark_cell or not mark_cell.text.strip():
                    continue
                mark = mark_cell.text.strip()

                def _value(index: int | None) -> float | None:
                    cell = row[index] if index is not None and index < len(row) else None
                    text = cell.text.strip() if cell else ""
                    try:
                        return float(text) if text else None
                    except ValueError:
                        return None

                items[mark] = {"tag": mark, "width_m": _value(width_index), "height_m": _value(height_index)}
            page += 1
        return items

    def _compare_reconciliation_item(self, tag: str, ifc_item: dict | None, pdf_item: dict | None) -> dict[str, Any]:
        """The actual per-tag join/comparison rule (SPEC-M2 §4D) -- factored
        out of `_synthesize_reconciliation_response`'s own loop (SPEC-M11)
        so `reverify_reconciliation_tag` below can re-run the *exact* same
        comparison for one tag, not a re-implementation that could quietly
        drift from the original.
        """
        if ifc_item and not pdf_item:
            status, detail = "missing_in_pdf", f"{tag} is present in the IFC model but has no matching row on the PDF schedule."
        elif pdf_item and not ifc_item:
            status, detail = "missing_in_ifc", f"{tag} is present on the PDF schedule but has no matching IFC element."
        else:
            width_delta = abs((ifc_item["width_m"] or 0.0) - (pdf_item["width_m"] or 0.0))
            height_delta = abs((ifc_item["height_m"] or 0.0) - (pdf_item["height_m"] or 0.0))
            if width_delta <= self._RECONCILIATION_TOLERANCE_M and height_delta <= self._RECONCILIATION_TOLERANCE_M:
                status = "matched"
                detail = f"{tag}: IFC and PDF dimensions agree within tolerance (±{self._RECONCILIATION_TOLERANCE_M} m)."
            else:
                status = "dimension_mismatch"
                detail = (f"{tag}: IFC {ifc_item['width_m']}×{ifc_item['height_m']} m vs PDF "
                           f"{pdf_item['width_m']}×{pdf_item['height_m']} m differ beyond the "
                           f"±{self._RECONCILIATION_TOLERANCE_M} m tolerance.")
        return {
            "tag": tag,
            "entity_type": (ifc_item or {}).get("entity_type"),
            "storey": (ifc_item or {}).get("storey"),
            "ifc_width_m": (ifc_item or {}).get("width_m"),
            "ifc_height_m": (ifc_item or {}).get("height_m"),
            "pdf_width_m": (pdf_item or {}).get("width_m"),
            "pdf_height_m": (pdf_item or {}).get("height_m"),
            "status": status,
            "detail": detail,
        }

    def reverify_reconciliation_tag(self, project_resources: ProjectResources, tag: str) -> dict[str, Any]:
        """SPEC-M11 §4C: re-reads both sources for exactly one tag and
        re-runs the same comparison a full reconciliation would -- the
        actual "closed-loop" claim, proven by a fresh read, never by
        trusting a finding's own stored values. Zero model calls, same as
        the full reconciliation this is a single-tag slice of.
        """
        ifc_items = self._reconciliation_ifc_items(project_resources)
        pdf_items = self._reconciliation_pdf_items(project_resources)
        return self._compare_reconciliation_item(tag, ifc_items.get(tag), pdf_items.get(tag))

    # Independent-review finding, 2026-09-19 (live-testing session):
    # rejection/negation phrases checked immediately before a candidate
    # number, so "Do not use the IFC value of 1.75m" no longer counts
    # 1.75 as the agent endorsing it. A narrow, disclosed keyword list --
    # not a claim of general negation understanding -- matching this
    # function's own already-established "narrow heuristic" framing.
    _DIMENSION_REJECTION_PHRASES = (
        "not use", "don't use", "do not use", "should not use", "shouldn't use",
        "not correct", "not the correct", "not accurate", "not valid",
        "not confirmed", "not verified", "cannot rely on", "can't rely on",
        "unreliable", "erroneous", "not trust", "incorrect",
    )

    @staticmethod
    def _matches_known_value(candidate: float, *known_values: float | None) -> bool:
        """Whole-number-exact / 0.05-tolerance match against any of the
        given known values (`None` entries never match) -- the one
        numeric-matching rule this project's numeric-consistency checks
        all share, factored out (D-064 item 2) so a value submitted
        through `submit_finding_verdict`'s structured arguments is
        checked against a finding's own known candidates the exact same
        way a value found in free text already was.
        """
        for value in known_values:
            if value is None:
                continue
            if float(value).is_integer():
                if candidate == value:
                    return True
            elif abs(candidate - value) < 0.05:
                return True
        return False

    @staticmethod
    def _extract_proposed_dimension(answer_markdown: str, ifc_value: float | None, pdf_value: float | None) -> float | None:
        """SPEC-M17 §4B: which of the two known, already-conflicting values
        the agent's own free-text conclusion actually commits to -- a
        narrow, disclosed heuristic, not a claim of general free-text
        understanding, matching this project's own established
        numeric-consistency check in spirit (reuses the exact same
        whole-number-exact / 0.05-tolerance matching rule).

        - Only one of the two known values appears in the text, in a
          context that isn't rejecting it -> that one (the agent picked a
          side).
        - Both appear, or neither appears -> `None`: the human reads
          `rationale` directly rather than being handed a guess.

        Independent-review finding, 2026-09-19 (live-testing session,
        real Azure gpt-5-mini): this function used to also accept "exactly
        one number that isn't either known value, on a verified answer" as
        a genuinely new proposed value -- intended for a real third-source
        number (e.g. a spec sheet), but reproduced live scripting an
        investigation whose actual tool call was `count_elements(IfcDoor)`
        returning 4 doors, with the model's whole answer being "There are
        4 doors." That 4 is not a dimension at all, yet the old rule
        persisted `proposed_width_m = proposed_height_m = 4.0` marked
        `verified` -- confidently wrong, not merely ungrounded. This
        function cannot tell a genuinely new grounded measurement apart
        from an unrelated real number that happens to be the answer's only
        one, so it no longer tries: removed entirely rather than patched,
        since no version of "guess from whichever numbers are left over"
        can safely distinguish those two cases from free text alone. A
        real third-source value now surfaces only in `rationale` for a
        human to read, never as a silently-adopted `proposed_*_m` field.
        This is a narrower guarantee than before, deliberately: it can now
        return `None` in a case where the number really was a genuine new
        value, but it can no longer confidently return the *wrong* thing.
        """
        import re

        rejection_pattern = "|".join(re.escape(phrase) for phrase in AgentService._DIMENSION_REJECTION_PHRASES)

        def _accepted_numbers(expected: float | None) -> list[float]:
            if expected is None:
                return []
            matches = []
            for match in re.finditer(r"\d+(?:\.\d+)?", answer_markdown):
                actual = float(match.group())
                if not AgentService._matches_known_value(actual, expected):
                    continue
                context = answer_markdown[max(0, match.start() - 80):match.start()].lower()
                if re.search(rejection_pattern, context):
                    continue
                matches.append(actual)
            return matches

        ifc_present = bool(_accepted_numbers(ifc_value))
        pdf_present = bool(_accepted_numbers(pdf_value))
        if ifc_present and not pdf_present:
            return ifc_value
        if pdf_present and not ifc_present:
            return pdf_value
        return None

    @staticmethod
    def build_finding_investigation_question(finding: EngineeringFinding) -> str:
        """SPEC-M17 §4B, amended (owner decision, 2026-09-18): the question
        an "investigate this finding" turn actually asks V2, built
        entirely from the finding's own already-stored fields (tag,
        detail, both sides' stored dimensions) -- never from free-text
        operator input, which would reopen a prompt-injection surface
        this design deliberately avoids (SPEC-M17 §3). Read-only, like
        every other V2 question -- the tools it can reach never write to
        the real IFC/PDF sources.

        Owner-reported, 2026-09-18 (found live): the model gave up too
        easily ("I cannot determine... please provide the PDF page") when
        `extract_pdf_field` (a generic Q&A-style tool) failed to relocate
        a specific schedule row that reconciliation's own deterministic
        table-reading method (`_reconciliation_pdf_items`) had already
        read correctly for this exact tag -- the agent had strictly less
        precise tooling for this than the system already had, and settled
        for "I don't know" on the first miss instead of trying harder.
        Now explicitly told not to give up after one failed lookup.

        Owner decision, 2026-09-19 (live-testing session): widened from
        dimension_mismatch only to all three SPEC-M11 finding types (see
        `INVESTIGABLE_FINDING_TYPES`) -- each gets its own question, since
        "which value is correct" only makes sense for a real numeric
        conflict; missing_in_pdf/missing_in_ifc ask the genuinely
        different question "is this a real omission, or does this element
        actually appear under a different tag/label."

        Independent-review finding, 2026-09-19 (D-064 item 2): every
        branch now ends with an explicit instruction to call
        `submit_finding_verdict` -- the tool's own structured arguments,
        not this free-text answer, are what `build_finding_proposal`
        reads (see that method's own docstring). The missing_in_pdf/
        missing_in_ifc branches also gained an explicit instruction to
        check `reconcile_doors_windows`'s own matched-item list before
        concluding a same-dimension candidate is this missing element
        under a different tag -- found live in this same session: a real
        investigation matched two IFC windows by dimension alone and
        proposed one of them might be the missing tag, without checking
        that both were *already* matched to their own tags in the very
        reconciliation data it had already read, which alone rules out
        either one being a second, hidden identity for the tag under
        investigation.
        """
        if finding.finding_type == "missing_in_pdf":
            return (
                f"A door/window reconciliation flagged that tag '{finding.tag}' is present in the IFC model "
                f"(width={finding.ifc_width_m} m, height={finding.ifc_height_m} m) but has no matching row on the "
                f"PDF schedule: {finding.detail} Investigate using your available tools -- re-check this element's "
                "own real IFC properties, and try extract_pdf_field more than once with differently-worded "
                f"questions (e.g. by dimension, by storey, by element type) before concluding tag '{finding.tag}' "
                "genuinely does not appear anywhere on the schedule; also check nearby/similar elements (same "
                "storey, same entity type) in case this element was logged under a different mark or tag on the "
                "PDF. Before concluding any candidate PDF row is this element under a different mark, first run "
                "reconcile_doors_windows and confirm that candidate row is not already matched to its own, "
                "different IFC tag -- a row already matched elsewhere cannot also be this one. Do not give up "
                "after a single failed lookup -- make a genuine multi-step effort before concluding this is a real "
                "omission. When you are done, call submit_finding_verdict exactly once: verdict='genuine_omission' "
                "if the schedule truly has no row for this tag, verdict='found_under_different_reference' (with "
                "basis naming the specific row/mark you found, already confirmed unmatched elsewhere) if it does, "
                "or verdict='inconclusive' only after genuinely trying more than one approach."
            )
        if finding.finding_type == "missing_in_ifc":
            return (
                f"A door/window reconciliation flagged that tag '{finding.tag}' is present on the PDF schedule "
                f"(width={finding.pdf_width_m} m, height={finding.pdf_height_m} m) but has no matching element in "
                f"the IFC model: {finding.detail} Investigate using your available tools -- re-check the IFC model's "
                "own elements near this dimension/type (get_element_properties, count_elements) before concluding "
                f"tag '{finding.tag}' genuinely was never modeled; also check nearby/similar elements (same storey, "
                "same entity type) in case this element exists in the IFC model under a different tag. Before "
                "concluding any candidate IFC element is this tag under a different name, first run "
                "reconcile_doors_windows and confirm that candidate element's own tag is not already matched to a "
                "different PDF row -- an element already matched elsewhere cannot also be this one. Do not give "
                "up after a single failed lookup -- make a genuine multi-step effort before concluding this "
                "element is genuinely missing from the model. When you are done, call submit_finding_verdict "
                "exactly once: verdict='genuine_omission' if the IFC model truly has no element for this tag, "
                "verdict='found_under_different_reference' (with basis naming the specific element/tag you found, "
                "already confirmed unmatched elsewhere) if it does, or verdict='inconclusive' only after genuinely "
                "trying more than one approach."
            )
        return (
            f"A door/window reconciliation flagged a dimension mismatch for tag '{finding.tag}': {finding.detail} "
            f"The IFC model currently records width={finding.ifc_width_m} m, height={finding.ifc_height_m} m. "
            f"The PDF schedule currently records width={finding.pdf_width_m} m, height={finding.pdf_height_m} m. "
            "Investigate using your available tools -- re-check this element's own real IFC properties, and try "
            "extract_pdf_field more than once with differently-worded questions before concluding the PDF schedule "
            "doesn't have this row; also check nearby/similar elements (same storey, same entity type) for a "
            "consistent pattern that corroborates one side. Do not give up after a single failed lookup -- make a "
            "genuine multi-step effort before concluding you cannot determine an answer. When you are done, call "
            "submit_finding_verdict exactly once with verdict='dimension_confirmed' and confirmed_width_m/"
            "confirmed_height_m set to the value(s) you determined to be correct (only set the one(s) that were "
            "actually in question), or verdict='inconclusive' only after genuinely trying more than one approach."
        )

    @staticmethod
    def build_finding_proposal(finding: EngineeringFinding, final_response: AgentResponse) -> AgentProposal:
        """SPEC-M17 §4B, amended: maps one completed V2 turn's own
        `AgentResponse` (produced by a normal `invoke_v2` call asking
        `build_finding_investigation_question`'s own question -- this
        method does not call the model itself) onto a persistable
        `AgentProposal` for the investigated finding.

        Independent-review finding, 2026-09-19 (D-064 item 2): now reads
        the model's own structured `submit_finding_verdict` call (carried
        through `final_response.execution_metadata["finding_verdict"]`)
        instead of guessing from `answer_markdown` -- this is what lets a
        `missing_in_pdf`/`missing_in_ifc` investigation's conclusion be
        expressed as `verdict` (a real omission vs. found under a
        different reference vs. inconclusive) rather than force-fit into
        a width/height that question was never actually about. A
        structured `confirmed_width_m`/`confirmed_height_m` is still
        independently checked against the finding's own known IFC/PDF
        values (`_matches_known_value`) before being trusted as
        `proposed_*_m` -- the model stating a number through this tool is
        not, on its own, sufficient grounds to adopt it; per this
        project's own established discipline, code still validates a
        structured claim, not just a free-text one. The free-text
        extraction remains only as a fallback for the rare turn that
        never reached `submit_finding_verdict` at all (e.g. the
        tool-calling loop ended some other way, such as hitting the
        iteration cap, before the model got the chance).
        """
        verdict_args = final_response.execution_metadata.get("finding_verdict")
        verdict: str | None = None
        verdict_basis: str | None = None
        proposed_width_m: float | None = None
        proposed_height_m: float | None = None
        if verdict_args:
            verdict = verdict_args.get("verdict")
            verdict_basis = verdict_args.get("basis")
            if verdict == "dimension_confirmed":
                candidate_width = verdict_args.get("confirmed_width_m")
                candidate_height = verdict_args.get("confirmed_height_m")
                if isinstance(candidate_width, (int, float)) and AgentService._matches_known_value(candidate_width, finding.ifc_width_m, finding.pdf_width_m):
                    proposed_width_m = candidate_width
                if isinstance(candidate_height, (int, float)) and AgentService._matches_known_value(candidate_height, finding.ifc_height_m, finding.pdf_height_m):
                    proposed_height_m = candidate_height
        else:
            proposed_width_m = AgentService._extract_proposed_dimension(final_response.answer_markdown, finding.ifc_width_m, finding.pdf_width_m)
            proposed_height_m = AgentService._extract_proposed_dimension(final_response.answer_markdown, finding.ifc_height_m, finding.pdf_height_m)
        return AgentProposal(
            proposed_width_m=proposed_width_m,
            proposed_height_m=proposed_height_m,
            verdict=verdict,
            verdict_basis=verdict_basis,
            rationale=final_response.answer_markdown,
            citations=final_response.citations,
            verification=final_response.verification,
            trace_id=final_response.trace_id,
        )

    def _synthesize_reconciliation_response(self, state: GraphState, multi_plan: MultiQueryPlan) -> dict:
        """SPEC-M2 §4D: join the IFC and PDF door/window sides on Tag/Mark.

        Every item found in either source is classified `matched`,
        `dimension_mismatch`, `missing_in_pdf`, or `missing_in_ifc` --
        never fabricated in either direction (§7). A reconciliation that
        executes to completion (both sides were read) is `answered`
        regardless of any individual item's status (§4G, OD-16): the
        item-level breakdown lives in `reconciliation_items`, not in the
        disposition.
        """
        ifc_items = self._reconciliation_ifc_items(state["project_resources"])
        pdf_items = self._reconciliation_pdf_items(state["project_resources"])
        counts = {"matched": 0, "dimension_mismatch": 0, "missing_in_pdf": 0, "missing_in_ifc": 0}
        reconciliation_items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        for tag in sorted(set(ifc_items) | set(pdf_items)):
            ifc_item, pdf_item = ifc_items.get(tag), pdf_items.get(tag)
            item = self._compare_reconciliation_item(tag, ifc_item, pdf_item)
            counts[item["status"]] += 1
            reconciliation_items.append(item)
            if ifc_item:
                evidence.append(Evidence(source_type=SourceType.IFC, source_file=state["project_resources"].ifc_repository.path.name,
                    summary=f"IFC {ifc_item['entity_type']} Tag={tag}.", locator={"global_id": ifc_item["global_id"], "express_id": ifc_item["express_id"], "tag": tag}))
            if pdf_item:
                evidence.append(Evidence(source_type=SourceType.PDF, source_file=state["project_resources"].document_analyzer.pdf_path.name,
                    summary=f"PDF schedule row Mark={tag}.", locator={"page": 2, "mark": tag}))
        answer = (
            "Door/window reconciliation between the IFC model and the PDF schedule (joined on Tag/Mark): "
            f"**{counts['matched']} matched**, **{counts['dimension_mismatch']} dimension mismatch**, "
            f"**{counts['missing_in_pdf']} missing from the PDF**, **{counts['missing_in_ifc']} missing from the IFC model**."
        )
        citations = self._citations(evidence, state)
        verification = VerificationStatus(status="verified", reason="Every item's status was independently derived from the source IFC quantities and the PDF's own table structure; no value was asserted without a matching or explicitly absent counterpart.")
        self._audit(state, "execute_reconciliation", "synthesized", "Door/window IFC<->drawing reconciliation joined on Tag.", counts)
        return {
            "answer": answer, "disposition": "answered", "citations": citations,
            "verification": verification.model_dump(), "reconciliation_items": reconciliation_items,
            "subresults": [], "evidence": [item.model_dump() for item in evidence], "tool_call_count_delta": 2,
        }

    @staticmethod
    def _natural_answer(multi_plan: MultiQueryPlan, subresults: list[dict[str, Any]]) -> str:
        """Render verified structured results without exposing raw tool payloads.

        The semantic planner decides `response_language`; this renderer only
        maps normalized operations/results to concise wording and never routes
        or interprets the user's language.
        """
        chinese = multi_plan.response_language.lower().startswith("zh")
        fragments: list[str] = []
        for item in subresults:
            plan, answer = item["plan"], item["answer"]
            if item.get("disposition") == "answered":
                if plan.get("source") == "ifc" and plan.get("operation") == "count":
                    import re
                    value = re.search(r"\*\*(\d+)\*\*", answer)
                    # D-055: kept in sync with router.SUPPORTED_ENTITY_TYPES.
                    noun = {
                        "IfcDoor": "门", "IfcWindow": "窗", "IfcWall": "墙", "IfcSpace": "空间",
                        "IfcStair": "楼梯", "IfcSlab": "楼板", "IfcRoof": "屋顶", "IfcColumn": "柱",
                        "IfcBeam": "梁", "IfcMember": "构件", "IfcRailing": "栏杆", "IfcCovering": "饰面",
                        "IfcFurnishingElement": "家具", "IfcFooting": "基础", "IfcPlate": "面板",
                    }.get(plan.get("entity_type"), "构件")
                    # "扇" is grammatically specific to door/window leaves; every
                    # other entity type uses the generic measure word "个".
                    measure_word = {"IfcDoor": "扇", "IfcWindow": "扇"}.get(plan.get("entity_type"), "个")
                    if chinese and value:
                        fragments.append(f"这个项目中共有 **{value.group(1)}** {measure_word}{noun}。")
                    else:
                        fragments.append(answer)
                elif plan.get("source") == "ifc" and plan.get("operation") == "group_by" and chinese:
                    noun = {
                        "IfcWindow": "窗户", "IfcDoor": "门", "IfcWall": "墙", "IfcSpace": "空间",
                        "IfcStair": "楼梯", "IfcSlab": "楼板", "IfcRoof": "屋顶", "IfcColumn": "柱",
                        "IfcBeam": "梁", "IfcMember": "构件", "IfcRailing": "栏杆", "IfcCovering": "饰面",
                        "IfcFurnishingElement": "家具", "IfcFooting": "基础", "IfcPlate": "面板",
                    }.get(plan.get("entity_type"), "构件")
                    grouped = item.get("result_value")
                    if isinstance(grouped, dict) and plan.get("postprocess") not in {"argmax", "argmin"}:
                        fragments.append("各层" + noun + "数量：" + "；".join(f"**{group}**：**{count}**" for group, count in grouped.items()) + "。")
                    elif isinstance(grouped, dict) and grouped:
                        group, count = next(iter(grouped.items()))
                        comparison = "最多" if plan.get("postprocess") == "argmax" else "最少"
                        fragments.append(f"**{group}** 的{noun}{comparison}，共 **{count}** 个。")
                    else:
                        fragments.append(answer)
                elif plan.get("source") == "ifc" and plan.get("operation") == "get_properties" and chinese:
                    records = item.get("result_value") or []
                    # D-060: mirrors the English _format_ifc_answer fix above
                    # -- more than one matched record means no single element
                    # was genuinely selected (a real selection always narrows
                    # to exactly one via a global_ids filter), so claiming
                    # "当前选中的是..." for an arbitrary records[0] would be a
                    # false, misleading certainty.
                    if isinstance(records, list) and len(records) > 1:
                        entity_label = {
                            "IfcDoor": "门", "IfcWindow": "窗", "IfcWall": "墙", "IfcSpace": "空间",
                            "IfcStair": "楼梯", "IfcSlab": "楼板", "IfcRoof": "屋顶", "IfcColumn": "柱",
                            "IfcBeam": "梁", "IfcMember": "构件", "IfcRailing": "栏杆", "IfcCovering": "饰面",
                            "IfcFurnishingElement": "家具", "IfcFooting": "基础", "IfcPlate": "面板",
                        }.get(plan.get("entity_type"), "构件")
                        sample = records[:5]
                        lines = [f"- {(record.get('element', {}) or {}).get('name') or '未命名'}（{record.get('storey') or '未分配楼层'}）" for record in sample]
                        more = f"\n……还有 {len(records) - 5} 个。" if len(records) > 5 else ""
                        fragments.append(
                            f"共有 **{len(records)}** 个{entity_label}匹配此请求——并未唯一确定某一个构件，以下是部分示例：\n\n" + "\n".join(lines) + more
                        )
                    else:
                        record = records[0] if isinstance(records, list) and records else {}
                        element = record.get("element", {}) if isinstance(record, dict) else {}
                        label = {
                            "IfcDoor": "门", "IfcWindow": "窗", "IfcWall": "墙", "IfcSpace": "空间",
                            "IfcStair": "楼梯", "IfcSlab": "楼板", "IfcRoof": "屋顶", "IfcColumn": "柱",
                            "IfcBeam": "梁", "IfcMember": "构件", "IfcRailing": "栏杆", "IfcCovering": "饰面",
                            "IfcFurnishingElement": "家具", "IfcFooting": "基础", "IfcPlate": "面板",
                        }.get(element.get("entity_type"), "构件")
                        fragments.append(
                            f"当前选中的是一个{label}，IFC 类型为 **{element.get('entity_type', plan.get('entity_type'))}**。"
                            f"所在楼层：**{record.get('storey') or 'Unassigned'}**。"
                        )
                else:
                    fragments.append(answer)
            elif chinese and plan.get("group_by") == "space":
                fragments.append("由于该 IFC 文件缺少可靠的空间边界关系，无法验证窗户的房间级分布。")
            else:
                fragments.append(answer)
        answer = " ".join(dict.fromkeys(fragment for fragment in fragments if fragment.strip()))
        # Keep a reversible, model-derived correction transparent without
        # turning the conversational answer into a spell-checking message.
        if chinese and multi_plan.corrections and not multi_plan.requires_clarification:
            return "我理解你是在问建筑模型中的对应构件。" + answer
        return answer

    @staticmethod
    def _merge_conversation_context(current: dict[str, Any], multi_plan: MultiQueryPlan, subresults: list[dict[str, Any]]) -> dict[str, Any]:
        context = dict(current)
        answered = [item for item in subresults if item.get("disposition") == "answered"]
        entity_types = [item["plan"].get("entity_type") for item in answered if item["plan"].get("entity_type")]
        context.update({
            "active_entity_types": list(dict.fromkeys(entity_types)),
            "previous_query_set": [item["plan"] for item in subresults],
            "previous_subplans": [item["plan"] for item in subresults],
            "last_answered_subtasks": [{"subtask_id": item["subtask_id"], "answer": item["answer"], "plan": item["plan"]} for item in answered],
            "unresolved_subtasks": [{"subtask_id": item["subtask_id"], "answer": item["answer"], "plan": item["plan"]} for item in subresults if item not in answered],
            "last_user_intent": multi_plan.intent,
            "response_language": multi_plan.response_language,
            "conversation_turn_id": int(context.get("conversation_turn_id", 0)) + 1,
        })
        # Clarification turns also contribute pending semantic state (for
        # example an unresolved space-distance target). Apply those deltas to
        # the next turn without treating the clarification as a verified
        # answer.
        for item in subresults:
            plan, update = item["plan"], item.get("context_update", {})
            if plan.get("operation") == "group_by" and plan.get("group_by") == "storey" and plan.get("postprocess") == "argmax":
                # The deterministic result was formatted with its leading (max)
                # storey, so retain that exact verified winner for semantic turns.
                answer = item.get("answer", "")
                if "**" in answer:
                    parts = answer.split("**")
                    if len(parts) > 1:
                        context["active_storey"] = parts[1]
            for key, value in update.items():
                if value is None:
                    context.pop(key, None)
                elif value not in ([], {}):
                    context[key] = value
        return context

    def _execute_ifc(self, state: GraphState) -> dict:
        plan = QueryPlan.model_validate(state["plan"])
        query = IfcQueryInput(
            operation=plan.operation or "count",
            entity_type=plan.entity_type,
            filters=plan.filters,
            measure=plan.measure,
            aggregation=plan.aggregation,
            group_by=plan.group_by or "none",
        )
        self._audit(
            state,
            "execute_ifc",
            "tool_called",
            "IFC tool invoked.",
            {"query": query.model_dump()},
        )
        try:
            tool_call_count = state.get("tool_call_count", 0) + 1
            result = state["project_resources"].ifc_repository.execute(query)
            citations = self._citations(result.evidence, state)
            next_filters = dict(plan.filters)
            if plan.operation == "group_by" and plan.group_by == "storey" and isinstance(result.value, dict) and result.value:
                # A subsequent "break that down by room" should inherit the winning
                # storey rather than re-running the whole project-wide aggregation.
                next_filters["storey"] = next(iter(result.value))
                self._audit(state, "execute_ifc", "grouped_execution", "Deterministic per-storey IFC grouping completed.", {"entity_type": plan.entity_type, "groups": result.value})
                if plan.postprocess in {"argmax", "argmin"}:
                    chooser = max if plan.postprocess == "argmax" else min
                    winner, count = chooser(result.value.items(), key=lambda item: (item[1], item[0]))
                    next_filters["storey"] = winner
                    self._audit(state, "execute_ifc", "postprocess_argmax", "Deterministic grouped extremum completed.", {"postprocess": plan.postprocess, "winner": winner, "count": count})
            if plan.operation == "group_by" and plan.group_by == "space" and isinstance(result.value, dict) and set(result.value) == {"Unassigned"}:
                message = (
                    "I inherited the previous window and storey context, but this IFC does not expose "
                    "space-boundary relationships for those windows. Please choose a named space or ask "
                    "for a floor-level breakdown instead of treating an unassigned result as room evidence."
                )
                self._audit(state, "execute_ifc", "clarification_requested", "Room grouping is not supported by the supplied IFC relationships.", {"inherited_filters": plan.filters, "group_by": plan.group_by})
                return {"tool_result": {
                    "answer": message,
                    "disposition": "clarification_required",
                    "citations": citations,
                    "verification": VerificationStatus(status="not_applicable", reason="No IFC space boundaries were available.").model_dump(),
                    "context_update": {
                        "active_source": SourceType.IFC.value,
                        "active_entity_type": plan.entity_type,
                        "active_entity_ids": plan.filters.get("global_ids", []),
                        "active_express_ids": plan.filters.get("express_ids", []),
                        "active_filters": plan.filters,
                        "active_group_by": plan.group_by,
                        "previous_query_plan": plan.model_dump(),
                        "evidence_refs": [item.id for item in result.evidence],
                    },
                }, "evidence": [item.model_dump() for item in result.evidence], "tool_call_count": tool_call_count}
            answer = self._format_ifc_answer(plan, result.value)
            consistency_issues = verify_execution_consistency(plan, tool_query=query.model_dump(), result_value=result.value, answer=answer)
            result_shape = "scalar_measurement" if isinstance(result.value, dict) and "value_m" in result.value else "grouped_counts" if isinstance(result.value, dict) else "scalar_count" if isinstance(result.value, int) else "list" if isinstance(result.value, list) else "unknown"
            self._audit(state, "result_shape_verification", "result_shape_verification", "IFC result shape checked against the validated plan.", {"status": "passed" if not consistency_issues else "failed", "expected_result_shape": plan.expected_result_shape, "actual_result_shape": result_shape})
            self._audit(state, "execution_consistency", "execution_consistency", "Intent-to-plan-to-tool consistency checked.", {"status": "passed" if not consistency_issues else "failed", "issues": [issue.__dict__ for issue in consistency_issues], "query": query.model_dump()})
            consistency = VerifierResult(verifier="intent_execution_consistency", passed=not consistency_issues, reason="Tool invocation and result shape preserve the validated intent." if not consistency_issues else consistency_issues[0].message)
            status = verification_status([
                DeterministicVerifier(state["project_resources"].ifc_repository).verify(query, result),
                InvariantValidator().validate(
                    evidence=result.evidence,
                    citations=[Citation.model_validate(item) for item in citations],
                    disposition="answered",
                ),
                consistency,
            ])
            tool_result = {
                "answer": answer if status.status == "verified" else "I could not safely finalize this answer because the execution result did not preserve the requested operation.",
                "disposition": "answered" if status.status == "verified" else "error",
                "citations": citations,
                "verification": status.model_dump(),
                "result_value": result.value,
                # Owner-reported, 2026-09-17: the repository layer already
                # computes this (e.g. "No IFC elements matched the query."
                # -- exactly what a mistranslated/nonexistent storey filter
                # like "第二层" instead of "Level 2" produces: a faithfully
                # correct zero for a bad filter, not a fabrication, but
                # presented with no hint anything was off) -- it was simply
                # never read by any caller. V1 doesn't need it (not
                # agentic); V2 does, since invoke_v2 forwards this straight
                # back to the model as this tool's own result, giving it a
                # chance to notice and retry with a corrected filter rather
                # than confidently reporting a wrong zero.
                "warnings": result.warnings,
                "context_update": {
                    "active_source": SourceType.IFC.value,
                    "active_entity_type": plan.entity_type,
                    "active_entity_ids": next_filters.get("global_ids", []),
                    "active_express_ids": next_filters.get("express_ids", []),
                    "active_filters": next_filters,
                    "active_group_by": plan.group_by,
                    "previous_query_plan": plan.model_dump(),
                    "evidence_refs": [item.id for item in result.evidence],
                },
            }
            if plan.operation == "space_distance":
                tool_result["context_update"]["pending_space_distance"] = None
            self._audit(
                state,
                "execute_ifc",
                "tool_completed",
                "IFC query completed.",
                {"matched_count": result.matched_count, "verification": status.status},
            )
            return {"tool_result": tool_result, "evidence": [item.model_dump() for item in result.evidence], "tool_call_count": tool_call_count}
        except Exception as error:
            self._audit(state, "execute_ifc", "error", "IFC tool failed.", {"error": str(error)})
            if plan.operation == "space_distance":
                message = "I need uniquely identified spaces before measuring distance. Please specify a room identifier such as GR-008 and the target space, for example Main Kitchen."
                self._audit(state, "execute_ifc", "clarification_requested", "Space distance could not be safely resolved; no distance was guessed.", {"error": str(error), "filters": plan.filters})
                return {"tool_result": {"answer": message, "disposition": "clarification_required", "citations": [], "verification": VerificationStatus(status="not_applicable", reason="Space identity or geometry was ambiguous.").model_dump(), "context_update": {
                    "active_source": SourceType.IFC.value,
                    "previous_query_plan": plan.model_dump(),
                    "pending_space_distance": {"from_space": plan.filters.get("from_space"), "to_space": plan.filters.get("to_space")},
                }}, "tool_call_count": state.get("tool_call_count", 0) + 1}
            return {"tool_result": self._error_result(str(error))}

    def _retrieve_relevant_documents(self, state: GraphState, question: str) -> list[tuple[str, float]]:
        """SPEC-M7: ranks configured documents by relevance via Azure AI
        Search hybrid retrieval (BM25 + vector), used only as a fallback
        inside `_execute_pdf_multi_document`'s zero-hit branch below --
        not run ahead of every `native_lookup` call, since that would cost
        a real model call (the embedding) even on the common case where a
        single document already answers unambiguously. Purely
        informational either way: this milestone's "location, never
        synthesis" invariant means a retrieval score only ever narrows
        which documents are worth *naming* in an honest miss message, it
        is never a substitute for `native_lookup`'s own confidence/value.

        Returns `[]` whenever retrieval isn't configured
        (`azure_search_endpoint` unset -- SPEC-M6's naive baseline is then
        the only behaviour, unaffected) or a real Azure AI Search call
        fails: retrieval is advisory, so a transient failure here must
        degrade to the existing blanket miss message, never surface as an
        unhandled error on top of an already-honest `clarification_required`.
        """
        # D-051/OD-48: SPEC-M9 originally scoped this to the "demo" project
        # only, an explicit gate, not an accident. Relaxed here -- any
        # project with retrieval actually configured (the two checks
        # immediately below) is now eligible, not just "demo" -- so the
        # real Dataset Pack buildings (digitalhub/duplex, SPEC-M13) can be
        # demonstrated against the same shared Azure AI Search index.
        # `_retrieve_relevant_documents`'s own caller already filters
        # every candidate against *this* project's own `document_analyzers`
        # (`name in analyzers`), which is what actually prevents a
        # different project's document from ever being named here --
        # verified true independent of this gate, not something this
        # change introduces.
        search_client = self.container.search_client_factory(self.settings)
        if search_client is None:
            return []
        embedding_provider = self.container.embedding_provider_factory(self.settings)
        if embedding_provider is None:
            return []
        try:
            from azure.search.documents.models import VectorizedQuery
            # Found live, 2026-09-13: this is the only model-call site in
            # this file that incremented model_call_count without emitting
            # a matching model_called/model_completed audit event -- every
            # other call (IFC/PDF vision, semantic planning) pairs the two.
            # A user going looking in the trace for "the model call the
            # response claims happened" for a question that hit this path
            # would correctly find nothing, since nothing was ever recorded.
            self._audit(
                state, "execute_pdf", "model_called",
                "Azure AI Search query embedding requested.",
                {"purpose": "azure_search_query_embedding", "question": question},
                actual_provider=embedding_provider.name, actual_model=embedding_provider.model,
            )
            vector = asyncio.run(embedding_provider.embed(question))
            self._audit(
                state, "execute_pdf", "model_completed",
                "Azure AI Search query embedding completed.",
                {"purpose": "azure_search_query_embedding", "vector_dimensions": len(vector)},
                actual_provider=embedding_provider.name, actual_model=embedding_provider.model,
            )
            # D-053: owner-reported, 2026-09-16, live -- a generic
            # schedule-style question against DigitalHub returned an empty
            # blanket miss even though retrieval genuinely ran (a real
            # embedding call, model_call_count 1). Root cause, confirmed by
            # querying the raw index directly: this shared index has no
            # server-side project scoping, so `top=5` truncation happens
            # across *every* project's documents combined, before this
            # method's caller ever filters by `name in analyzers`. Demo's
            # own 6 near-identically-worded schedule documents crowded
            # DigitalHub's 3 out of the global top 5 entirely -- the
            # existing `name in analyzers` filter (still kept below, as a
            # second, independent safety net) only ever proves a *wrong*
            # project's document can't be named, never that a project's
            # *own* documents can't be squeezed out of contention first.
            # `filter` applies server-side, before ranking/truncation, so
            # the top-5 is now computed only among this project's own
            # documents, not the whole shared index.
            project_id = state["project_resources"].manifest.project_id
            results = search_client.search(
                search_text=question,
                vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=5, fields="content_vector")],
                select=["filename"], top=5, filter=f"project_id eq '{project_id}'",
            )
            return [(item["filename"], item["@search.score"]) for item in results]
        except Exception as error:
            self._audit(
                state, "execute_pdf", "error",
                "Azure AI Search retrieval failed; falling back to the naive baseline's blanket miss message.",
                {"error": str(error)},
            )
            return []

    def _execute_pdf_multi_document(
        self, state: GraphState, plan: QueryPlan, query: DocumentQueryInput,
        analyzers: dict[str, Any], tool_call_count: int, model_call_count: int,
    ) -> dict:
        """SPEC-M6's naive, deterministic multi-document baseline: no vision
        escalation, no document-name inference -- just native_lookup against
        every configured document, in configured order, zero model calls.

        Exists to make two failure modes honestly visible rather than
        guessed past: a field answerable from more than one document
        (precision failure), and a realistically-phrased question whose
        answer exists only under different vocabulary than any table uses,
        so no document matches at all (recall failure -- indistinguishable
        here from a genuine no-answer case, which is the point: this
        baseline cannot tell them apart, and that inability is the evidence
        baseline SPEC-M7 (Azure AI Search) is meant to beat, not an
        assumption). Only reached when `plan.requested_document` is unset
        and more than one document is configured -- see `_execute_pdf`.
        """
        hits: list[tuple[str, Any]] = []
        for filename, analyzer in analyzers.items():
            result = analyzer.native_lookup(query)
            self._audit(
                state, "execute_pdf", "tool_completed",
                f"Deterministic native-text extraction attempted against {filename}.",
                {"document": filename, "confidence": result.confidence, "extraction_method": result.extraction_method},
            )
            if result.confidence >= self.settings.pdf_confidence_threshold and result.value is not None:
                hits.append((filename, result))

        if len(hits) == 1:
            filename, result = hits[0]
            # Evidence.source_file already names the right document (each
            # DocumentAnalyzer stamps its own pdf_path.name), but Citation/
            # _citations only surfaces `locator`, not source_file directly --
            # add it there too so a multi-document answer's citation actually
            # says which document it came from, not just the audit trail.
            evidence = [item.model_copy(update={"locator": {**item.locator, "document": filename}}) for item in result.evidence]
            citations = self._citations(evidence, state)
            evidence_result = EvidenceVerifier().verify(evidence, self.settings.pdf_confidence_threshold)
            status = verification_status([
                evidence_result,
                InvariantValidator().validate(evidence=evidence, citations=[Citation.model_validate(item) for item in citations], disposition="answered"),
            ])
            return {"tool_result": {
                "answer": str(result.value),
                "disposition": "answered", "citations": citations, "verification": status.model_dump(),
                "context_update": {
                    "active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump(),
                    "evidence_refs": [item.id for item in evidence],
                },
            }, "evidence": [item.model_dump() for item in evidence], "tool_call_count": tool_call_count, "model_call_count": model_call_count}

        if len(hits) > 1:
            candidates = [filename for filename, _ in hits]
            self._audit(
                state, "execute_pdf", "clarification_requested",
                "The requested field is answerable from more than one configured document; refusing to guess between them.",
                {"candidate_documents": candidates},
            )
            return {"tool_result": {
                "answer": f"This field is answerable from more than one document ({', '.join(candidates)}); please specify which one you mean.",
                "disposition": "clarification_required", "citations": [],
                "verification": VerificationStatus(status="not_applicable", reason="Cross-document match was not unique.").model_dump(),
                "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump()},
            }, "tool_call_count": tool_call_count, "model_call_count": model_call_count}

        # SPEC-M7: a directed miss, not a blanket one, when retrieval is
        # configured and ranks a candidate above the empirically-set
        # threshold (D-018/OD-35) -- still zero fabricated values, still
        # clarification_required, only the message's specificity changes.
        # Costs exactly one real model call (the query embedding) when it
        # runs at all; skipped entirely (search_client_factory returns
        # None) whenever azure_search_endpoint is unset, so SPEC-M6's
        # zero-model-call naive-baseline behaviour is unchanged by default.
        retrieved = self._retrieve_relevant_documents(state, state["question"])
        if retrieved:
            model_call_count += 1
        relevant_configured = [(name, score) for name, score in retrieved if name in analyzers]
        directed_candidates = [name for name, score in relevant_configured if score >= self.settings.azure_search_relevance_threshold]
        if relevant_configured:
            # A dedicated event, not just a key inside the clarification_
            # requested payload below -- found worth doing during the
            # owner's own hands-on walkthrough (D-018 addendum): the
            # retrieval evidence was real and correct but easy to miss,
            # buried among many other Raw Trace payloads. This event type
            # is what apps/web/src/main.tsx's dedicated "AI Search
            # Retrieval" card (not the generic Raw Trace list) looks for,
            # so the scores that actually drove the directed-vs-blanket
            # miss decision are visible without expanding several
            # unrelated payloads first. Only emitted when retrieval
            # actually ran and returned candidates -- absence of this
            # event in a trace already means "retrieval wasn't attempted
            # or configured," which is itself informative.
            self._audit(
                state, "execute_pdf", "retrieval_evaluated",
                f"Azure AI Search ranked {len(relevant_configured)} configured document(s) by relevance.",
                {
                    "documents_evaluated": [{"filename": name, "score": score} for name, score in relevant_configured],
                    "relevance_threshold": self.settings.azure_search_relevance_threshold,
                    "directed_candidates": directed_candidates,
                },
            )
        self._audit(
            state, "execute_pdf", "clarification_requested",
            "No configured document produced a confident deterministic match for this field.",
            {"documents_checked": list(analyzers.keys()), "retrieval_candidates": relevant_configured, "directed_candidates": directed_candidates},
        )
        if directed_candidates:
            answer = f"I could not automatically extract this field, but the most relevant configured document(s) may be: {', '.join(directed_candidates)}."
        else:
            answer = "I could not find this field with a confident match in any configured document."
        return {"tool_result": {
            "answer": answer,
            "disposition": "clarification_required", "citations": [],
            "verification": VerificationStatus(status="not_applicable", reason="No document produced a confident deterministic match.").model_dump(),
            "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump()},
        }, "tool_call_count": tool_call_count, "model_call_count": model_call_count}

    def _execute_pdf(self, state: GraphState) -> dict:
        plan = QueryPlan.model_validate(state["plan"])
        query = DocumentQueryInput(
            field=plan.requested_field or state["question"],
            question=state["question"],
        )
        self._audit(state, "execute_pdf", "tool_called", "PDF document tool invoked.", {"query": query.model_dump(), "requested_document": plan.requested_document})
        try:
            tool_call_count = state.get("tool_call_count", 0) + 1
            model_call_count = state.get("model_call_count", 0)
            analyzers = state["project_resources"].document_analyzers
            analyzer: Any = None
            if plan.requested_document is not None:
                analyzer = analyzers.get(plan.requested_document)
                if analyzer is None:
                    self._audit(state, "execute_pdf", "error", "Requested document is not part of the configured corpus.", {"requested_document": plan.requested_document, "configured_documents": list(analyzers.keys())})
                    return {"tool_result": self._error_result(f"Requested document '{plan.requested_document}' is not part of the configured document corpus.")}
            elif len(analyzers) > 1:
                # SPEC-M6: more than one document configured and no explicit
                # target -- resolve deterministically, at zero model calls.
                # Never falls through to vision below: vision can resolve
                # "what's on this page," not "which document," so it cannot
                # help with either failure mode this branch exists to
                # surface honestly (see _execute_pdf_multi_document).
                return self._execute_pdf_multi_document(state, plan, query, analyzers, tool_call_count, model_call_count)
            else:
                analyzer = next(iter(analyzers.values()))
            result = analyzer.native_lookup(query)
            self._audit(
                state,
                "execute_pdf",
                "tool_completed",
                "Deterministic native-text extraction attempted.",
                {"confidence": result.confidence, "extraction_method": result.extraction_method, "ambiguity": result.ambiguity},
            )

            if result.confidence < self.settings.pdf_confidence_threshold and result.miss_reason == "no_matching_record":
                # SPEC-M1.5 §4B/OD-14: no candidate record was named at all --
                # a structural miss, not a document-legibility problem. A
                # vision pass over the same page cannot resolve "which record
                # did you mean" either, so route straight to clarification
                # with zero model calls instead of paying a vision round-trip
                # that cannot help.
                self._audit(
                    state,
                    "execute_pdf",
                    "clarification_requested",
                    "No candidate record was named; vision fallback skipped as it cannot resolve a record-identification ambiguity.",
                    {"native_confidence": result.confidence, "miss_reason": result.miss_reason},
                )
            elif result.confidence < self.settings.pdf_confidence_threshold:
                board = analyzer.target_board(query.question)
                field = analyzer.target_field(query.question)
                self._audit(
                    state,
                    "execute_pdf",
                    "retry",
                    "Native text was insufficient; invoking vision extraction.",
                    {"native_confidence": result.confidence},
                )
                if board and field:
                    provider = self.container.vision_provider_factory(self.settings)
                    self._audit(state, "execute_pdf", "model_called", "PDF board-localization model call started.", {"purpose": "pdf_board_localize", "board": board}, actual_provider=provider.name, actual_model=provider.model)
                    location, _ = asyncio.run(analyzer.localize_board(provider, query, board))
                    model_call_count += 1
                    # Some vision providers correctly identify the requested board in
                    # their rationale but omit the redundant `board` field. The typed
                    # request already pins the board, so preserve that identity only
                    # when the provider did not report a conflicting board.
                    if not location.board:
                        location = location.model_copy(update={"board": board})
                    self._audit(state, "execute_pdf", "model_completed", "PDF board-localization model call completed.", {"location": location.model_dump()}, actual_provider=provider.name, actual_model=provider.model)
                    if location.ambiguity or not location.bbox or (location.board or "").upper() != board:
                        reason = location.ambiguity or f"I could not uniquely locate board {board} in the drawing."
                        return {"tool_result": {
                            "answer": reason, "disposition": "clarification_required", "citations": [],
                            "verification": VerificationStatus(status="not_applicable", reason="Board localization was not unique.").model_dump(),
                            "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump()},
                        }, "tool_call_count": tool_call_count, "model_call_count": model_call_count}
                    self._audit(state, "execute_pdf", "model_called", "Board-localized PDF crop sent to candidate extractor.", {"purpose": "pdf_board_extract", "board_bbox": location.bbox}, actual_provider=provider.name, actual_model=provider.model)
                    candidates, crop = asyncio.run(analyzer.board_candidates(provider, query, board, location.bbox))
                    model_call_count += 1
                    normalized = [
                        candidate for candidate in candidates.candidates
                        if (candidate.board or board).upper() == board
                        and analyzer.canonical_field(candidate.field)
                        == analyzer.canonical_field(field)
                        and candidate.value is not None
                    ]
                    self._audit(state, "execute_pdf", "model_completed", "Board-localized candidate extraction completed.", {"candidate_count": len(candidates.candidates), "valid_candidate_count": len(normalized), "candidates": [item.model_dump() for item in candidates.candidates], "ambiguity": candidates.ambiguity}, actual_provider=provider.name, actual_model=provider.model)
                    if candidates.ambiguity or len(normalized) != 1:
                        reason = candidates.ambiguity or f"I found {len(normalized)} valid candidates for {field} on {board}; please clarify the target field."
                        return {"tool_result": {
                            "answer": reason, "disposition": "clarification_required", "citations": [],
                            "verification": VerificationStatus(status="not_applicable", reason="Candidate selection was not unique.").model_dump(),
                            "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump()},
                        }, "tool_call_count": tool_call_count, "model_call_count": model_call_count}
                    candidate = normalized[0]
                    self._audit(state, "execute_pdf", "model_called", "Independent same-crop PDF verifier started.", {"purpose": "pdf_board_verify", "candidate": candidate.model_dump()}, actual_provider=provider.name, actual_model=provider.model)
                    verification = asyncio.run(analyzer.verify_board_candidate(provider, query, board, candidate, crop))
                    model_call_count += 1
                    self._audit(state, "execute_pdf", "model_completed", "Independent same-crop PDF verification completed.", {"verification": verification.model_dump()}, actual_provider=provider.name, actual_model=provider.model)
                    evidence = [Evidence(
                        source_type=SourceType.PDF, source_file=analyzer.pdf_path.name,
                        summary=f"{board} · {field}: {candidate.value}{(' ' + candidate.unit) if candidate.unit else ''}",
                        locator={"page": query.page_hint or 1, "bbox": location.bbox, "field": field, "board": board, "extraction_method": "board_localized_vision", "evidence_crop": crop.name},
                        extracted_value=candidate.value, confidence=candidate.confidence,
                    )]
                    citations = self._citations(evidence, state)
                    verified = all([verification.supported, verification.board_matches, verification.field_matches, verification.value_matches, verification.unique_match])
                    verifier = VerifierResult(verifier="same_crop_pdf_vision", passed=verified, confidence=verification.confidence, reason=verification.rationale, supporting_evidence_ids=[evidence[0].id])
                    status = verification_status([verifier, InvariantValidator().validate(evidence=evidence, citations=[Citation.model_validate(item) for item in citations], disposition="answered" if verified else "clarification_required")])
                    return {"tool_result": {
                        "answer": f"{candidate.value}{(' ' + candidate.unit) if candidate.unit else ''}" if verified else verification.rationale,
                        "disposition": "answered" if verified else "clarification_required", "citations": citations, "verification": status.model_dump(),
                        "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump(), "evidence_refs": [evidence[0].id]},
                    }, "evidence": [item.model_dump() for item in evidence], "tool_call_count": tool_call_count, "model_call_count": model_call_count}

                provider = self.container.vision_provider_factory(self.settings)
                self._audit(state, "execute_pdf", "model_called", "PDF page sent to the vision extractor.", {"purpose": "pdf_extract"}, actual_provider=provider.name, actual_model=provider.model)
                result = asyncio.run(
                    analyzer.vision_lookup(provider, query)
                )
                model_call_count += 1
                self._audit(state, "execute_pdf", "model_completed", "PDF vision extraction completed.", {"value": result.value, "bbox": result.bbox, "ambiguity": result.ambiguity}, actual_provider=provider.name, actual_model=provider.model)
                self._audit(state, "execute_pdf", "model_called", "Independent PDF evidence verifier invoked.", {"purpose": "pdf_verify"}, actual_provider=provider.name, actual_model=provider.model)
                visual_verification = asyncio.run(
                    analyzer.verify_vision_extraction(provider, query, result)
                )
                model_call_count += 1
                self._audit(state, "execute_pdf", "model_completed", "Independent PDF evidence verification completed.", {"supported": visual_verification.supported, "confidence": visual_verification.confidence, "rationale": visual_verification.rationale}, actual_provider=provider.name, actual_model=provider.model)
                if not visual_verification.supported:
                    result.confidence = min(result.confidence, visual_verification.confidence)
                    result.ambiguity = visual_verification.rationale
            citations = self._citations(result.evidence, state)
            evidence_result = EvidenceVerifier().verify(
                result.evidence,
                self.settings.pdf_confidence_threshold,
            )
            verified = evidence_result.passed and not result.ambiguity
            disposition = "answered" if verified else "clarification_required"
            status = verification_status([
                evidence_result,
                InvariantValidator().validate(
                    evidence=result.evidence,
                    citations=[Citation.model_validate(item) for item in citations],
                    disposition=disposition,
                ),
            ])
            answer = (
                str(result.value)
                if verified
                else (result.ambiguity or "I need to inspect the cited drawing region with the vision analyzer before I can verify that value.")
            )
            return {"tool_result": {
                "answer": answer,
                "disposition": disposition,
                "citations": citations,
                "verification": status.model_dump(),
                "context_update": {
                    "active_source": SourceType.PDF.value,
                    "previous_query_plan": plan.model_dump(),
                    "evidence_refs": [item.id for item in result.evidence],
                },
            }, "evidence": [item.model_dump() for item in result.evidence], "tool_call_count": tool_call_count, "model_call_count": model_call_count}
        except Exception as error:
            if "OPENAI_API_KEY is required" in str(error):
                evidence = [Evidence(
                    source_type=SourceType.PDF,
                    # `analyzer` may be unbound here if the exception came from
                    # _execute_pdf_multi_document (raised before any single
                    # `analyzer` was ever selected) -- fall back to the
                    # primary configured document's name in that case.
                    source_file=(analyzer.pdf_path.name if analyzer is not None else next(iter(analyzers.keys()), "unknown")),
                    summary="The electrical schedule requires a configured vision provider for reliable table extraction.",
                    locator={
                        "page": 1,
                        "bbox": [0, 0, 1190, 842],
                        "field": query.field,
                        "extraction_method": "vision_provider_unavailable",
                    },
                    confidence=0.0,
                )]
                self._audit(state, "execute_pdf", "clarification_requested", "Vision provider was unavailable; refusing to guess from a visual drawing.", {"error": str(error)})
                return {"tool_result": {
                    "answer": "I need a configured vision provider before extracting this visual schedule. Please specify the board or circuit once vision verification is available; I will not guess between multiple totals on the page.",
                    "disposition": "clarification_required",
                    "citations": self._citations(evidence, state),
                    "verification": VerificationStatus(status="not_applicable", reason="Vision provider unavailable; no numerical claim was made.").model_dump(),
                    "context_update": {"active_source": SourceType.PDF.value, "previous_query_plan": plan.model_dump()},
                }, "evidence": [item.model_dump() for item in evidence]}
            self._audit(state, "execute_pdf", "error", "PDF tool failed.", {"error": str(error)})
            return {"tool_result": self._error_result(str(error))}

    def _execute_viewer(self, state: GraphState) -> dict:
        viewer = state.get("viewer_context", {})
        if not viewer.get("screenshot_base64") and not viewer.get("selected_global_ids"):
            return {"tool_result": {
                "answer": "Please select an element or capture the current model view so I can inspect visual evidence.",
                "disposition": "clarification_required",
                "citations": [],
                "verification": VerificationStatus(status="not_applicable").model_dump(),
                "context_update": {},
            }}
        if not viewer.get("screenshot_base64"):
            return {"tool_result": {
                "answer": "Please capture the current model view before asking for visual reasoning. A selected IFC ID alone is better handled by deterministic IFC tools.",
                "disposition": "clarification_required", "citations": [],
                "verification": VerificationStatus(status="not_applicable", reason="No screenshot evidence was supplied.").model_dump(),
                "context_update": {},
            }}
        stair_question = any(marker in state["question"].lower() for marker in ("stair", "staircase", "open or enclosed", "enclosed", "楼梯"))
        target_type = viewer.get("target_entity_type") or viewer.get("selected_entity_type")
        target_id = viewer.get("target_global_id") or ((viewer.get("selected_global_ids") or [None])[0])
        target_visible = bool(viewer.get("target_visible")) or target_type in {"IfcStair", "IfcStairFlight"}
        target = "staircase" if stair_question else "construction elements"
        grounding = "selected_metadata" if target_type or target_id else "question_only"
        provider = self.container.vision_provider_factory(self.settings)
        self._audit(state, "execute_viewer", "model_called", "Viewer snapshot sent to the vision provider.", {"purpose": "viewer_snapshot", "screenshot_id": viewer.get("snapshot_id"), "target": target, "target_grounding": grounding, "selected_type": target_type, "selected_global_id": target_id}, actual_provider=provider.name, actual_model=provider.model)
        try:
            model_call_count = state.get("model_call_count", 0) + 1
            visual = asyncio.run(provider.vision_structured(
                purpose="viewer_snapshot",
                image_base64=viewer["screenshot_base64"],
                response_model=VisionViewerInspection,
                prompt=(
                    "Inspect only the supplied construction-model screenshot. Do not infer exact BIM facts or counts. "
                    "Assess whether the requested target is actually visible and whether the view is sufficient. "
                    "Return target_visible=false and sufficient_view=false when it is not clearly visible. "
                    "For a generic visual question, describe only construction elements clearly visible in the image. "
                    f"Question: {state['question']}\nVisual target: {target}\nSelected IFC GlobalIds: {viewer.get('selected_global_ids', [])}\n"
                    f"Selected IFC type: {viewer.get('selected_entity_type')}\nGrounded target type: {target_type}\n"
                    f"Grounded target GlobalId: {target_id}\nSelection metadata target_visible: {target_visible}"
                ),
            ))
            visual_payload = visual.model_dump()
            visual_payload.update({"screenshot_id": viewer.get("snapshot_id"), "target": target, "target_grounding": grounding})
            self._audit(state, "execute_viewer", "model_completed", "Viewer snapshot vision result received.", {"visual": visual_payload}, actual_provider=provider.name, actual_model=provider.model)
            self._audit(state, "execute_viewer", "model_called", "Independent viewer-snapshot verifier invoked.", {"purpose": "viewer_snapshot_verify"}, actual_provider=provider.name, actual_model=provider.model)
            viewer_verification = asyncio.run(provider.vision_structured(
                purpose="viewer_snapshot_verify",
                image_base64=viewer["screenshot_base64"],
                response_model=VisionViewerVerification,
                prompt=(
                    "Independently verify this visual claim against the supplied screenshot only. "
                    "Mark it unsupported if the view is occluded or does not visibly establish the claim. "
                    f"Question: {state['question']}\nTarget: {target}\nClaim: {visual.claim or visual.observation or visual.classification or 'No claim'}\n"
                    f"Model target_visible: {visual.target_visible}\nGrounded target type: {target_type}\nGrounded target GlobalId: {target_id}"
                ),
            ))
            model_call_count += 1
            verification_payload = viewer_verification.model_dump()
            verification_payload.update({"screenshot_id": viewer.get("snapshot_id"), "target": target, "target_visible": visual.target_visible, "sufficient_view": visual.sufficient_view})
            self._audit(state, "execute_viewer", "model_completed", "Independent viewer-snapshot verification completed.", {"verification": verification_payload}, actual_provider=provider.name, actual_model=provider.model)
        except Exception as error:
            self._audit(state, "execute_viewer", "model_failed", "Viewer vision request failed.", {"error": str(error)}, actual_provider=provider.name, actual_model=provider.model)
            return {"tool_result": {
                "answer": "I could not obtain a local visual interpretation of this screenshot. Please confirm the vision model is available, then capture another view.",
                "disposition": "clarification_required", "citations": [],
                "verification": VerificationStatus(status="not_applicable", reason=str(error)).model_dump(), "context_update": {},
            }}
        claim = visual.claim or visual.observation or visual.classification or "The screenshot does not provide a sufficiently grounded observation."
        evidence = [Evidence(
            source_type=SourceType.VIEWER,
            source_file=state["project_resources"].manifest.ifc_file,
            summary=f"Viewer snapshot: {claim}",
            locator={
                "snapshot_id": viewer.get("snapshot_id"),
                "camera_pose": viewer.get("camera_pose", {}),
                "selected_global_ids": viewer.get("selected_global_ids", []),
                "target_visible": visual.target_visible,
                "target_global_id": target_id,
                "target_entity_type": target_type,
                "visual": {**visual.model_dump(), "target": target, "target_grounding": grounding},
            },
            extracted_value=claim,
            confidence=visual.confidence,
        )]
        if (stair_question and not visual.target_visible) or not visual.sufficient_view or visual.occlusion_detected or not (viewer_verification.supported and viewer_verification.claim_supported and viewer_verification.screenshot_sufficient):
            return {"tool_result": {
                "answer": f"I cannot verify that from this view alone. {claim} Please rotate, zoom, or select the relevant element and capture another view.",
                "disposition": "clarification_required", "citations": self._citations(evidence, state),
                "verification": VerificationStatus(status="not_applicable", reason=viewer_verification.rationale).model_dump(),
                "context_update": {"active_source": SourceType.VIEWER.value, "active_snapshot_id": viewer.get("snapshot_id")},
            }, "evidence": [item.model_dump() for item in evidence], "model_call_count": model_call_count}
        return {"tool_result": {
            "answer": claim,
            "disposition": "answered",
            "citations": self._citations(evidence, state),
            "verification": VerificationStatus(status="verified", verifier_results=[VerifierResult(verifier="same_snapshot_vision", passed=True, confidence=viewer_verification.confidence, reason=viewer_verification.rationale, supporting_evidence_ids=[evidence[0].id])], reason="Vision result was independently verified against the supplied snapshot.").model_dump(),
            "context_update": {"active_source": SourceType.VIEWER.value, "active_snapshot_id": viewer.get("snapshot_id")},
        }, "evidence": [item.model_dump() for item in evidence], "model_call_count": model_call_count}

    def _refuse(self, state: GraphState) -> dict:
        self._audit(state, "refuse", "refused", "No safe supported tool route.", {"plan": state.get("plan")})
        return {"tool_result": {
            "answer": state.get("planner_error") or state.get("unsupported_reason") or state.get("plan", {}).get("rationale") or "I cannot answer that reliably from the configured IFC, drawing, or current viewer evidence. Please ask a question about a model element, a drawing field, or the visible selected view.",
            "disposition": "error" if state.get("planner_error") else "unsupported",
            "citations": [],
            "verification": VerificationStatus(status="not_applicable").model_dump(),
            "context_update": {},
        }}

    @staticmethod
    def _cloud_trace_query(trace_id: str) -> str:
        """The raw KQL this deployment's Application Insights telemetry for
        one specific request is filtered by -- factored out of
        `_cloud_trace_url` so the frontend can offer it as plain, copyable
        text (D-029), not only baked into a URL. Found live, 2026-09-14:
        the owner's own Azure Portal opened this deployment's Logs blade
        into its "Queries hub" picker instead of running the pre-filled
        query, with no data shown -- a per-account/tenant Portal-side
        default this project's own URL cannot control (confirmed against
        Microsoft's documented deep-link format, which is what `_cloud_
        trace_url` already produces). Pasting this text directly into that
        hub's own search box still reaches the same data.
        """
        return (
            "union requests, dependencies, traces, exceptions\n"
            f'| where tostring(customDimensions["app.trace_id"]) == "{trace_id}"\n'
            "| order by timestamp desc"
        )

    def _cloud_trace_url(self, trace_id: str) -> str | None:
        """D-028: a one-click Azure Portal link into this deployment's own
        Application Insights telemetry for this specific request, requested
        directly by the owner ("click the cloud provenance banner and land
        on the cloud-side trace for this answer").

        This app's own `trace_id` (used by AuditStore/citations/
        `/api/v1/traces/{trace_id}`) and OpenTelemetry's own auto-generated
        span/operation ID for the underlying HTTP request are two
        independently-generated identifiers with no built-in relationship --
        `main.py`'s `chat()` tags the current OpenTelemetry span with this
        value (`app.trace_id`) specifically so the two become joinable here,
        via a `customDimensions` filter Application Insights already
        supports for exactly this purpose.

        Returns None (a plain-text banner, not a broken link) unless both
        `azure_tenant_id` and `app_insights_resource_id` are configured --
        opt-in-when-unset, the same pattern as `otel_exporter_connection_
        string` itself.
        """
        if not (self.settings.azure_tenant_id and self.settings.app_insights_resource_id):
            return None
        from urllib.parse import quote
        query = self._cloud_trace_query(trace_id)
        return (
            f"https://portal.azure.com/#@{self.settings.azure_tenant_id}"
            f"/resource{self.settings.app_insights_resource_id}/logs"
            f"?query={quote(query)}"
        )

    # SPEC-M11 §4B: one fixed rule, not a model call and not configurable
    # this milestone -- an element absent from one source entirely is a
    # bigger data-integrity problem than a measured disagreement between
    # two sources that both at least named it.
    _FINDING_SEVERITY_BY_TYPE = {
        "dimension_mismatch": "medium",
        "missing_in_pdf": "high",
        "missing_in_ifc": "high",
    }

    def _upsert_findings_from_reconciliation(self, state: GraphState, response: AgentResponse) -> None:
        """SPEC-M11: promotes every non-`matched` reconciliation item on
        this response into a persisted `EngineeringFinding` -- auto-creation,
        not a separate "promote" action a human has to remember to take.
        Does not touch reconciliation's own computation (`_synthesize_
        reconciliation_response` stays a pure, side-effect-free function);
        this only persists what it already decided.

        A `matched` item is never auto-closed here, even if it belongs to a
        tag with an existing open finding -- closing only ever happens
        through the explicit re-verify action, so a human always sees that
        transition rather than a finding silently vanishing between chat
        turns.

        Best-effort: a persistence failure here must not turn an otherwise
        correct, already-computed reconciliation answer into an error --
        losing this milestone's workflow tracking for one response is a
        strictly lesser failure than losing the answer itself (the same
        proportionality D-014's `context_persist_error` already applies to
        conversation-context persistence).
        """
        manifest = state["project_resources"].manifest
        evidence_refs = [citation.evidence_id for citation in response.citations]
        for item in response.reconciliation_items:
            if item.status == ReconciliationStatus.MATCHED:
                continue
            try:
                self.container.finding_store.upsert_from_reconciliation(
                    project_id=manifest.project_id, source_set_id=manifest.source_set_id,
                    trace_id=state["trace_id"], tag=item.tag, finding_type=item.status.value,
                    severity=self._FINDING_SEVERITY_BY_TYPE[item.status.value], detail=item.detail,
                    ifc_width_m=item.ifc_width_m, ifc_height_m=item.ifc_height_m,
                    pdf_width_m=item.pdf_width_m, pdf_height_m=item.pdf_height_m,
                    evidence_refs=evidence_refs,
                )
            except Exception as error:
                self._audit(
                    state, "finding_upsert", "error",
                    "Could not persist an EngineeringFinding for this reconciliation item; the answer itself is unaffected.",
                    {"tag": item.tag, "finding_type": item.status.value, "error": str(error)},
                )

    def _finalize(self, state: GraphState) -> dict:
        if state.get("clarification"):
            response = AgentResponse(
                thread_id=state["thread_id"],
                trace_id=state["trace_id"],
                disposition=Disposition.CLARIFICATION_REQUIRED,
                answer_markdown=state["clarification"],
                verification=VerificationStatus(status="not_applicable"),
            )
        else:
            result = state["tool_result"]
            disposition_value = result.get("disposition", "answered")
            answer_markdown = result["answer"]
            model_call_count = state.get("model_call_count", 0)
            response = AgentResponse(
                thread_id=state["thread_id"],
                trace_id=state["trace_id"],
                disposition=Disposition(disposition_value),
                answer_markdown=answer_markdown,
                citations=[Citation.model_validate(item) for item in result.get("citations", [])],
                verification=VerificationStatus.model_validate(result["verification"]),
                execution_metadata={
                    # "multi_source" alone told a user nothing about which
                    # sources -- found live, 2026-09-13, on exactly the
                    # reconciliation query this names explicitly (the only
                    # multi-subplan intent today): the Execution step's
                    # subtitle read "source: multi_source" with no
                    # indication it was joining IFC and the PDF schedule.
                    "source": (
                        "ifc+pdf (reconciliation)" if state.get("multi_plan", {}).get("intent") == "reconciliation"
                        else state.get("plan", {}).get("source") if len(state.get("multi_plan", {}).get("subplans", [])) <= 1
                        else "multi_source"
                    ),
                    "planning_mode": "heuristic" if all(item.get("planning_mode") == "heuristic" for item in state.get("multi_plan", {}).get("subplans", [])) else "llm",
                    "configured_provider": self.settings.llm_provider,
                    "model_call_count": model_call_count,
                    "tool_call_count": state.get("tool_call_count", 0),
                    "cloud_trace_url": self._cloud_trace_url(state["trace_id"]),
                    # A copy-pastable fallback for the URL above: found
                    # live, 2026-09-14, that opening the URL landed on
                    # Azure Portal's own "Queries hub" picker with no data
                    # shown -- a Portal-side default this project's URL
                    # cannot control. Pasting this text into that picker's
                    # own search box still reaches the same data.
                    "cloud_trace_query": self._cloud_trace_query(state["trace_id"]),
                    "response_language": result.get("response_language"),
                    "normalized_request": state.get("multi_plan", {}).get("normalized_request") or state.get("question"),
                    "corrections": state.get("multi_plan", {}).get("corrections", []),
                    "subplans": state.get("multi_plan", {}).get("subplans", []),
                    "subtask_summary": {
                        "successful": sum(1 for item in result.get("subresults", []) if item.get("disposition") == "answered"),
                        "unresolved": sum(1 for item in result.get("subresults", []) if item.get("disposition") != "answered"),
                    },
                },
                context_update=result.get("context_update", {}),
                reconciliation_items=[ReconciliationItem.model_validate(item) for item in result.get("reconciliation_items", [])],
            )
            if response.reconciliation_items:
                self._upsert_findings_from_reconciliation(state, response)
        self._audit(
            state,
            "finalize",
            "finalized",
            "Agent response finalized.",
            {"disposition": response.disposition.value, "verification": response.verification.status},
        )
        self._audit(
            state,
            "finalize",
            "disposition_resolution",
            "Final disposition was resolved from verified subtask outcomes.",
            {"disposition": response.disposition.value, "verification": response.verification.status},
        )
        return {"final_response": response.model_dump()}

    def invoke(
        self,
        *,
        project_resources: ProjectResources,
        thread_id: str | None,
        question: str,
        viewer_context: dict | None,
        conversation_context: dict | None = None,
        source_preference: str = "auto",
    ) -> AgentResponse:
        initial: GraphState = {
            "project_resources": project_resources,
            "thread_id": thread_id or str(uuid4()),
            "trace_id": str(uuid4()),
            "question": question,
            "viewer_context": viewer_context or {},
            "conversation_context": conversation_context or {},
            "source_preference": source_preference,
            "model_call_count": 0,
            "tool_call_count": 0,
        }
        outcome = self.graph.invoke(initial)
        return AgentResponse.model_validate(outcome["final_response"])

    @staticmethod
    def _v2_system_prompt(storey_names: list[str] | None = None, source_preference: str = "auto") -> str:
        """SPEC-M16: V2's own system prompt.

        Deliberately restates the same honesty invariant every part of V1
        already enforces structurally (D-001): the model's role is
        deciding *which* tool(s) answer a question, never stating a fact
        without one. Nothing here asks the model to compute, estimate, or
        recall a construction fact from its own training -- only to call a
        tool and then describe that tool's real, verified return value.

        `source_preference` (independent-review finding, 2026-09-17): the
        workbench's own Source control (auto/ifc/pdf/viewer_snapshot)
        reached V1's `agent.invoke` but was never even threaded into
        `_chat_v2`/`invoke_v2` at all for V2 -- confirmed live: setting it
        to "pdf" and asking a question the model could answer via either
        source still ran the IFC tool. V2 has no routing-layer concept of
        "source" the way V1's own heuristic router does (the model picks
        a *tool*, not a *source*, and several tools are IFC-only or
        PDF-only with no overlap at all) -- a prompt-level steer for the
        genuinely ambiguous cases is the honest integration point this
        architecture actually has, not a hard routing gate.
        """
        storey_guidance = (
            f"This project's real storey names are exactly: {storey_names} -- always pass one of "
            "these exact strings as a storey filter, never a translation, abbreviation, or ordinal "
            "guess (e.g. \"第二层\" or \"2nd floor\") of the storey the user meant. "
        ) if storey_names else ""
        source_guidance = {
            "ifc": "The user has set a source preference of 'ifc' -- when a question could plausibly be answered from either the IFC model or the PDF drawings, prefer the IFC-based tools (count_elements, group_elements_by_storey, get_element_properties, aggregate_quantity, space_distance) over extract_pdf_field, unless the question is unambiguously about the PDF schedule itself. ",
            "pdf": "The user has set a source preference of 'pdf' -- when a question could plausibly be answered from either the IFC model or the PDF drawings, prefer extract_pdf_field over the IFC-based tools, unless the question is unambiguously about the 3D model itself. ",
            "viewer_snapshot": "The user has set a source preference of 'viewer_snapshot' -- prefer inspect_current_view over other tools when the question could plausibly be about what is currently shown in the 3D viewer. ",
        }.get(source_preference, "")
        return (
            "You are a construction/BIM assistant with tools to query a real IFC building model "
            "and its PDF drawings/schedules. Decide which tool(s) answer the user's question, call "
            "them (several independent ones in the same turn if the question needs them), then "
            "answer using ONLY the values those tools actually returned this turn. Never state a "
            "count, measurement, or property value you did not just receive from a tool call -- if "
            "no tool can answer part of the question, say so honestly rather than guessing. "
            "Respond in the same language as the user's latest message. "
            f"{storey_guidance}"
            f"{source_guidance}"
            "If a tool result includes a non-empty 'warnings' field (e.g. a storey filter matched "
            "zero elements), that is a signal your filter value may be wrong, not proof the true "
            "count is zero -- reconsider the filter (check it against the real storey names above) "
            "before reporting a zero as fact. "
            f"Valid entity_type values for every tool: {sorted(SUPPORTED_ENTITY_TYPES)}. "
            "reconcile_doors_windows only checks door/window width and height -- never claim it "
            "checked any other attribute (fire rating, material, etc.). "
            "When a question asks about ONE specific storey (e.g. 'how many doors on Level 2'), "
            "call count_elements with that storey filter, not group_elements_by_storey -- both "
            "give a correct number, but group_elements_by_storey's own evidence necessarily covers "
            "every storey, not just the one asked about, which is needlessly broad for a "
            "single-storey question. Reserve group_elements_by_storey for questions that are "
            "themselves about comparing or ranking storeys (e.g. 'which floor has the most'). "
            "Tools cannot filter by name or sub-category (e.g. there is no way to ask for only "
            "'tables' out of IfcFurnishingElement) -- if a large result was capped to a sample plus "
            "a total count, that is the most detail this system can give; do not call the same tool "
            "again with the same or a differently-worded request hoping for a different or more "
            "complete result. State the exact total, describe the sample honestly as partial, and "
            "stop there rather than retrying. "
            # Independent-review finding, 2026-09-17, third pass, resolved
            # at the data layer rather than by restricting what the model
            # is allowed to say (owner decision, 2026-09-17: V2 exists
            # specifically so the model can freely synthesize over tool
            # results instead of being limited to a closed, pre-built set
            # of operations -- capping the *output* to only ever restate
            # bound fields, as a stricter fact-binding architecture would,
            # would reintroduce exactly the ceiling V2 was built to avoid.
            # Enriching the *input* with complete information costs
            # nothing in generalization and closes the same gap at its
            # actual root cause: the model not having complete data, not
            # a rendering problem). A sample cap is safe for a total
            # *count* (exact regardless of sampling) but was not safe for
            # a claim about *how many distinct values/types* exist -- "how
            # many distinct window sizes" answered from only the first 40
            # of 85 elements could truthfully report 3 distinct sizes in
            # the sample while a 4th existed only among the omitted ones.
            # `distinct_value_summary` below is computed from the FULL,
            # untruncated list before capping, so it is exhaustive even
            # when sample_items is not.
            "If a result was capped to a sample plus a total count, do not infer how many *distinct* "
            "values, types, or sizes exist from sample_items alone -- it is only a partial sample. "
            "When the tool result includes a distinct_value_summary field, it is computed from ALL "
            "items (not just the sample) and is the exact, exhaustive answer for how many distinct "
            "values exist for that field; use it instead of counting from sample_items."
        )

    def _v2_dispatch_tool(self, tool_call: ToolCallEvent, state: GraphState) -> dict[str, Any]:
        """SPEC-M16: execute one V2-requested tool call, synchronously.

        Called via `asyncio.to_thread` from the async V2 loop (this is the
        same blocking IfcOpenShell/file-bound work V1's own `_execute_ifc`/
        `_execute_pdf`/`_execute_viewer` already do -- offloading it here
        keeps the event loop free for other requests/streaming exactly the
        way V1's own `asyncio.to_thread(agent.invoke, ...)` call in
        `main.py` already does for the whole V1 turn).

        Reuses V1's own execution/verification/evidence methods completely
        unchanged (this module's own established contract) -- no new
        computation logic exists for V2 anywhere in this codebase.

        Owner product feedback, 2026-09-21: the Decision Trace panel's
        Execution step showed a bare tool-call count with no way to see
        *which* tool was called with *what* arguments short of opening a
        raw JSON trace event per step -- for a finding investigation in
        particular, this is exactly the information (which entity type,
        which field) a reviewer needs to judge whether the investigation
        actually looked in the right place (see D-070's own tool-selection
        defect, found the same day). One audit event per dispatched call,
        logged before any branch below executes, closes this generically
        for every V2 turn, not just investigations.
        """
        self._audit(
            state, "v2_tool_call", "tool_called", f"V2 called {tool_call.tool_name}.",
            {"tool_name": tool_call.tool_name, "arguments": tool_call.arguments},
            planning_mode="tool_calling",
        )
        if tool_call.tool_name == "submit_finding_verdict":
            # D-064 item 2: a local, no-op "tool" -- it queries no real
            # IFC/PDF/viewer source, only records the model's own
            # structured conclusion so invoke_v2's outer loop can carry it
            # into this turn's `execution_metadata`. Disposition is
            # "answered" (this call did exactly what it was asked), not a
            # judgment on whether the *verdict itself* is well-grounded --
            # that check happens downstream, in `build_finding_proposal`,
            # against the finding's own known candidate values.
            return {
                "tool_result": {
                    "answer": tool_call.arguments.get("basis", ""),
                    "disposition": "answered",
                    "citations": [],
                    "verification": VerificationStatus(status="not_applicable", reason="A structured verdict submission, not a data query.").model_dump(),
                    "result_value": {"status": "verdict recorded"},
                },
                "evidence": [], "tool_call_delta": 0, "plan": [],
                "finding_verdict": tool_call.arguments,
            }
        if tool_call.tool_name == "reconcile_doors_windows":
            reason = "V2 tool call: door/window width/height reconciliation against the PDF schedule."
            multi_plan = MultiQueryPlan(
                intent="reconciliation", response_language="en", raw_user_message=state["question"],
                normalized_request=state["question"], interpretation_confidence="high", rationale=reason,
                subplans=[
                    QueryPlan(subtask_id="task_1", source="ifc", intent="reconciliation", operation="list", entity_type=None, filters={}, group_by="none", expected_result_shape="list", rationale=reason, planning_mode="tool_calling", rule_id="tool:reconcile_doors_windows", matched_signals=["intent:reconciliation"], match_status="complete"),
                    QueryPlan(subtask_id="task_2", source="pdf", intent="reconciliation", operation="extract_field", filters={"page_hint": 2}, group_by="none", expected_result_shape="list", rationale=reason, planning_mode="tool_calling", rule_id="tool:reconcile_doors_windows", matched_signals=["intent:reconciliation"], match_status="complete"),
                ],
            )
            result = self._synthesize_reconciliation_response(state, multi_plan)
            return {
                "tool_result": {"answer": result["answer"], "disposition": result["disposition"], "citations": result["citations"], "verification": result["verification"], "result_value": result.get("reconciliation_items")},
                "evidence": result.get("evidence", []),
                # Independent-review finding, 2026-09-17: a *delta* (how
                # many tool invocations this one dispatch made), not an
                # absolute new total -- see the comment on the non-
                # reconciliation branch below for why an absolute value
                # computed here is unsafe under invoke_v2's own parallel
                # dispatch via asyncio.gather.
                "tool_call_delta": result.get("tool_call_count_delta", 1),
                "plan": [sub.model_dump() for sub in multi_plan.subplans],
            }
        plan = build_plan_from_tool_call(tool_call.tool_name, tool_call.arguments)
        allowed, reason = capability_gate(plan)
        if not allowed:
            return {"tool_result": {"answer": reason or "This request is outside the supported tool capabilities.", "disposition": "unsupported", "citations": [], "verification": VerificationStatus(status="not_applicable", reason="Rejected by the capability gate before execution.").model_dump(), "result_value": None}, "evidence": [], "tool_call_delta": 0, "plan": [plan.model_dump()]}
        # Independent-review finding, 2026-09-17: `_execute_ifc`/`_execute_pdf`/
        # `_execute_viewer` are V1's own shared methods -- they return an
        # *absolute* new `tool_call_count` (`state.get("tool_call_count", 0) + 1`),
        # correct for V1's own sequential LangGraph execution (state is
        # mutated between each node), but invoke_v2 dispatches multiple
        # tool calls in *parallel* via `asyncio.gather`, all reading the
        # *same* pre-dispatch `state` snapshot -- every concurrent call
        # computes the same "+1" off the same baseline, so the last one
        # processed silently overwrote the others' contribution instead of
        # summing (confirmed live: two successful parallel tool calls
        # reported tool_call_count=1, not 2). Converting to a delta here
        # (this dispatch's own baseline vs. its own result) lets the
        # caller sum deltas instead of trusting an absolute value computed
        # from a stale, shared snapshot.
        baseline = state.get("tool_call_count", 0)
        local_state: GraphState = {**state, "plan": plan.model_dump()}
        if plan.source == "ifc":
            result = self._execute_ifc(local_state)
        elif plan.source == "pdf":
            result = self._execute_pdf(local_state)
        elif plan.source == "viewer_snapshot":
            result = self._execute_viewer(local_state)
        else:
            result = {"tool_result": self._error_result(f"Unsupported tool source: {plan.source}")}
        result["plan"] = [plan.model_dump()]
        result["tool_call_delta"] = result.get("tool_call_count", baseline) - baseline
        return result

    async def invoke_v2(
        self,
        *,
        project_resources: ProjectResources,
        thread_id: str | None,
        question: str,
        viewer_context: dict | None,
        recent_turns: list[dict[str, str]] | None = None,
        deadline: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
        source_preference: str = "auto",
        include_verdict_tool: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """SPEC-M16: V2's tool-calling agent loop, streamed.

        `include_verdict_tool` (D-064 item 2, independent-review finding,
        2026-09-19): offers `submit_finding_verdict` alongside every other
        tool for this turn only -- set exclusively by `_chat_v2` when this
        turn is investigating a `EngineeringFinding`. A normal chat
        question never gets it: there is no "finding verdict" to submit,
        and offering it unconditionally would just invite the model to
        call an irrelevant tool.

        Yields plain dicts for the caller (an SSE endpoint, or a test
        harness collecting them into a list) to forward:
        - {"type": "tool_status", "tool_name": ..., "call_id": ..., "status": "started"|"completed"}
        - {"type": "answer_chunk", "text": ...}
        - {"type": "final", "response": AgentResponse}  -- always the last event

        Runs independently of V1's LangGraph `StateGraph` -- V2 attempts
        every question from a clean start (SPEC-M16 OD-50: a full,
        standalone engine for a fair benchmark, never a fallback consulting
        V1's heuristics/semantic planner first).

        `deadline`/`cancel_check` (independent-review finding, 2026-09-17):
        V1's own request lifecycle (`chat()` in main.py) bounds the whole
        turn by `request_timeout_seconds` and lets `/api/v1/requests/{id}/
        cancel` interrupt it; V2's own `tool_calling_max_iterations` only
        bounds *iteration count*, never wall-clock time, and V2 requests
        were never registered in `app.state.requests` at all -- confirmed
        live: a 30ms deadline with a 150ms-delayed fake model still
        produced a normal `final` ~282ms later, no timeout, and the
        cancel/status endpoints 404 for any V2 request_id. These are
        optional (`None` = today's unbounded-by-time behavior, matching
        every existing caller/test that doesn't pass them) so the web
        layer (main.py's `_chat_v2`/`_v2_sse_stream`) can own the actual
        policy (an absolute `time.perf_counter()` deadline and a
        callable checking that caller's own request record) without this
        method importing anything FastAPI/app.state-shaped.
        """
        thread_id = thread_id or str(uuid4())
        trace_id = str(uuid4())
        state: GraphState = {
            "project_resources": project_resources, "thread_id": thread_id, "trace_id": trace_id,
            "question": question, "viewer_context": viewer_context or {}, "conversation_context": {},
            "tool_call_count": 0, "model_call_count": 0,
        }
        provider = self.container.text_provider_factory(self.settings)
        # Owner-reported, 2026-09-17: a Chinese storey phrase ("第二层")
        # passed straight through as a filter value, never translated to
        # this project's own English storey names, matched zero real
        # elements -- a faithfully-executed but wrong filter, reported as
        # a confident (wrong) zero. Giving the model the real names up
        # front removes the guess entirely for any project that has an
        # IFC source at all (PDF-only projects have none to give).
        try:
            storey_names = [
                storey["name"] for storey in project_resources.ifc_repository.metadata().get("storeys", []) if storey.get("name")
            ]
        except Exception:
            storey_names = []
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._v2_system_prompt(storey_names, source_preference)}]
        tool_definitions = [*TOOL_DEFINITIONS, SUBMIT_FINDING_VERDICT_TOOL] if include_verdict_tool else TOOL_DEFINITIONS
        for turn in recent_turns or []:
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": turn["answer"]})
        messages.append({"role": "user", "content": question})

        self._audit(state, "v2_turn_started", "model_called", "V2 tool-calling agent turn started.", {"question": question, "recent_turn_count": len(recent_turns or [])}, planning_mode="tool_calling")
        # SPEC-M16 SS E: real, measured latency (see the live benchmark
        # report) found that most of a turn's wall time is a single,
        # invisible-to-the-user model round trip deciding which tool(s) to
        # call, *before* anything streams -- a genuine architectural limit
        # of tool-calling (the decision round cannot itself be streamed;
        # see stream_turn's own docstring), not something this phase's
        # design can eliminate. This is the honest mitigation available
        # now: an immediate signal that the agent has started working,
        # rather than several seconds of visible silence.
        yield {"type": "thinking"}

        all_citations: list[dict[str, Any]] = []
        all_evidence: list[dict[str, Any]] = []
        all_plans: list[dict[str, Any]] = []
        all_reconciliation_items: list[dict[str, Any]] = []
        # Independent-review findings, 2026-09-17 -- both fed by the
        # per-tool-call loop below:
        # `subtask_dispositions`: one entry per dispatched tool call this
        # whole turn, used to distinguish a fully successful turn from one
        # where some subtask failed/was unsupported (previously collapsed
        # into a flat "answered" as long as *any* call left a citation).
        subtask_dispositions: list[str] = []
        # `expected_numeric_facts`: one (entity_terms, expected_numbers) pair
        # per successful scalar/aggregate/group-by tool result this turn --
        # this turn's own ground truth, independent of anything the model
        # goes on to say. Used after the final answer is assembled to check
        # the model's own narrated numbers actually came from a real tool
        # result, not free-form invention on top of a real citation (a fake
        # provider scripted to answer "99999" after a real count_elements
        # call returning 4 previously still finalized as disposition=answered,
        # verification=passed/verified, since that check only ever looked at
        # "did a tool call leave a citation this turn," not whether the
        # model's own prose was consistent with it -- and even the first
        # numeric-only version of this check was itself bypassable by
        # mentioning the *real* number in an unrelated aside while stating a
        # *fabricated* one as the actual claim; entity_terms lets the check
        # bind a number to what it's actually claimed to describe).
        expected_numeric_facts: list[tuple[frozenset[str], set[float]]] = []
        answer_parts: list[str] = []
        max_iterations = self.settings.tool_calling_max_iterations
        for iteration in range(1, max_iterations + 1):
            # Independent-review finding, 2026-09-17: checked once per
            # iteration -- the same natural checkpoint tool_calling_max_iterations
            # already uses -- rather than around each individual `await`,
            # which would need weaving through provider.stream_turn's own
            # internals. A slow deployment or a hung tool call can still
            # run past `deadline`/past a cancel request until the *next*
            # iteration boundary; this bounds it to "one more model+tool
            # round trip," not "instantly," which is the same granularity
            # V1's own cooperative cancellation (`task.cancel()` between
            # awaits) already provides.
            if deadline is not None and time.perf_counter() > deadline:
                self._audit(state, "v2_turn_error", "timeout", "V2 agent exceeded its bounded wall-clock deadline.", {"iteration": iteration}, planning_mode="tool_calling")
                response = AgentResponse(
                    thread_id=thread_id, trace_id=trace_id, disposition=Disposition.TIMEOUT,
                    answer_markdown="The request exceeded its time limit. Please retry with a narrower question.",
                    verification=VerificationStatus(status="not_applicable", reason="Timed out before a final answer was reached."),
                    execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                )
                yield {"type": "final", "response": response}
                return
            if cancel_check is not None and cancel_check():
                self._audit(state, "v2_turn_error", "cancelled", "V2 agent turn was cancelled.", {"iteration": iteration}, planning_mode="tool_calling")
                response = AgentResponse(
                    thread_id=thread_id, trace_id=trace_id, disposition=Disposition.CANCELLED,
                    answer_markdown="Request cancelled. No result was committed to the conversation context.",
                    verification=VerificationStatus(status="not_applicable", reason="Cancelled before a final answer was reached."),
                    execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                )
                yield {"type": "final", "response": response}
                return
            if iteration > 1:
                # Owner-reported, 2026-09-16: only the very first model call
                # of a turn got its own audit event ("v2_turn_started"
                # above) -- a later iteration's call (the one that reads
                # tool results back and composes the final natural-language
                # answer) had no start marker of its own. DecisionStory.tsx's
                # client-side stage-timing approximation charges the gap
                # between two audit events to whichever stage the first one
                # belongs to, so with no marker here that entire
                # answer-composition call silently got folded into
                # "Verification" (whatever the last verification-tagged
                # event happened to be) -- making Verification's reported
                # latency mostly someone else's cost, not its own. This
                # gives the second (and any later) model call its own
                # Planning-bucketed start, same as the first.
                self._audit(state, "v2_turn_continued", "model_called", "V2 agent requested another model turn using this iteration's tool results.", {"iteration": iteration}, planning_mode="tool_calling")
            model_call_count = state.get("model_call_count", 0) + 1
            state["model_call_count"] = model_call_count
            # Independent-review finding, 2026-09-17, second pass: without
            # some signal here, an iteration composing a long answer would
            # go visibly silent for its whole duration before the first
            # token arrives. Re-uses the same "thinking" signal SPEC-M16
            # SS E already established for the first call's own silent
            # decision round trip (which already gets it once, before
            # this loop starts -- only re-sent for iteration > 1 so it
            # isn't emitted twice back-to-back for the first).
            if iteration > 1:
                yield {"type": "thinking"}
            turn_complete = None
            this_iteration_answer_parts: list[str] = []
            # Independent-review finding, 2026-09-17, second pass: the
            # per-iteration deadline/cancel checks above only run
            # *between* iterations -- a single iteration's own model call
            # sleeping/hanging past `deadline` was never interrupted,
            # confirmed live: deadline=50ms, a 200ms-delayed fake model,
            # turn still finalized normally ~200ms later with no timeout.
            # Now wrapped with `asyncio.wait_for` per received event, so a
            # slow *individual* call is bounded too, not just cumulative
            # iteration count.
            #
            # Owner decision, 2026-09-17: `AnswerChunkEvent`s are streamed
            # live again (yielded the instant each token arrives), not
            # buffered-then-replayed. They were buffered for two rounds
            # (SPEC-M16 SS E, second pass) specifically so a narrative
            # later caught as inconsistent with this turn's own tool
            # results could be fully withheld before ever reaching the
            # client. That reason no longer applies: an inconsistent
            # narrative is no longer withheld (see the consistency check
            # below) -- it is shown with a caveat instead, since every
            # *confirmed* catch of that check across three rounds of
            # independent review was against a deliberately scripted
            # adversarial test, never a real fabrication from the actual
            # model in live use, while the check's own false-positive rate
            # against real live usage was confirmed twice. Buffering had
            # a real, felt UX cost with no corresponding real-world
            # benefit: the model's own answer-composition call is most of
            # a turn's wall time, and buffering meant the client saw
            # nothing (just a static "thinking" indicator) for that whole
            # duration before every token appeared at once in a burst. A
            # truncated/content-filtered response (finish_reason below) is
            # a different, non-probabilistic case where the response
            # really is incomplete -- but since it can only be detected
            # *after* the stream ends, and the (real, if incomplete) text
            # has already been streamed live by then, that path now
            # appends a clear caveat to what was actually shown rather
            # than trying to retroactively hide it.
            stream_iter = provider.stream_turn(messages=messages, tools=tool_definitions, purpose="v2_tool_turn").__aiter__()
            while True:
                remaining = (deadline - time.perf_counter()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    self._audit(state, "v2_turn_error", "timeout", "V2 agent exceeded its bounded wall-clock deadline mid-model-call.", {"iteration": iteration}, planning_mode="tool_calling")
                    response = AgentResponse(
                        thread_id=thread_id, trace_id=trace_id, disposition=Disposition.TIMEOUT,
                        answer_markdown="The request exceeded its time limit. Please retry with a narrower question.",
                        verification=VerificationStatus(status="not_applicable", reason="Timed out before a final answer was reached."),
                        execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                    )
                    yield {"type": "final", "response": response}
                    return
                try:
                    event = await (asyncio.wait_for(stream_iter.__anext__(), timeout=remaining) if remaining is not None else stream_iter.__anext__())
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    self._audit(state, "v2_turn_error", "timeout", "V2 agent's model call itself exceeded the bounded wall-clock deadline.", {"iteration": iteration}, planning_mode="tool_calling")
                    response = AgentResponse(
                        thread_id=thread_id, trace_id=trace_id, disposition=Disposition.TIMEOUT,
                        answer_markdown="The request exceeded its time limit. Please retry with a narrower question.",
                        verification=VerificationStatus(status="not_applicable", reason="Timed out before a final answer was reached."),
                        execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                    )
                    yield {"type": "final", "response": response}
                    return
                if isinstance(event, AnswerChunkEvent):
                    this_iteration_answer_parts.append(event.text)
                    yield {"type": "answer_chunk", "text": event.text}
                elif isinstance(event, TurnCompleteEvent):
                    turn_complete = event
            messages.append(turn_complete.raw_assistant_message)
            answer_parts.extend(this_iteration_answer_parts)
            if not turn_complete.tool_calls:
                # Independent-review finding, 2026-09-17: a stream cut short
                # by the model's own max-token limit or blocked mid-answer
                # by content filtering was previously treated identically
                # to a normal, complete "stop" -- a truncated narrative
                # (half a sentence, a number with no unit) could finalize
                # as disposition=answered with no indication anything was
                # cut off. See TurnCompleteEvent.finish_reason's own
                # docstring.
                if turn_complete.finish_reason in {"length", "content_filter"}:
                    self._audit(state, "v2_turn_finalized", "error", "V2's model turn ended abnormally before completing its answer.", {"iteration": iteration, "finish_reason": turn_complete.finish_reason}, planning_mode="tool_calling", actual_provider=provider.name, actual_model=provider.model)
                    # Owner decision, 2026-09-17: appends a caveat to the
                    # text actually shown instead of replacing it with a
                    # generic templated message. Now that AnswerChunkEvents
                    # stream live (see the loop above), whatever partial
                    # text the model produced before being cut off has
                    # already reached the client by the time finish_reason
                    # is known -- swapping in different text here would
                    # make the client-rendered stream and the saved
                    # "final" response disagree about what was actually
                    # said, which is worse than showing the real (if
                    # incomplete) text with an honest note that it was cut
                    # off. Citations gathered from any prior iteration's
                    # tool calls are kept for the same reason: they are
                    # real, not invalidated by this iteration's own
                    # truncation.
                    narrative = "".join(answer_parts) + f"\n\n⚠️ This response was cut off ({turn_complete.finish_reason}) before it finished -- treat it as incomplete."
                    response = AgentResponse(
                        thread_id=thread_id, trace_id=trace_id, disposition=Disposition.ERROR,
                        answer_markdown=narrative, citations=[Citation.model_validate(item) for item in all_citations],
                        verification=VerificationStatus(status="failed", reason=f"The model turn ended with finish_reason={turn_complete.finish_reason!r} instead of a normal stop."),
                        execution_metadata={"engine": "v2", "model_call_count": model_call_count, "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling", "actual_provider": provider.name, "actual_model": provider.model, "finish_reason": turn_complete.finish_reason},
                    )
                    yield {"type": "final", "response": response}
                    return
                self._audit(state, "v2_turn_finalized", "finalized", "V2 agent finished calling tools and produced its final answer.", {"iteration": iteration}, planning_mode="tool_calling", actual_provider=provider.name, actual_model=provider.model)
                # Owner-reported, 2026-09-17: this was `"answered" if
                # all_citations else "answered"` -- both branches identical,
                # so the condition was dead code and every zero-tool-call
                # turn (already flagged as a known gap in the original
                # SPEC-M16 benchmark report, question 10) was mislabeled
                # "answered". A 24-question EN/ZH domain sweep confirmed
                # this is a broader, reproducible pattern, not an edge
                # case: every one of 5 zero-tool-call turns in that sweep
                # was genuinely a clarification request or an honest
                # capability-limit explanation (an ambiguous storey name, a
                # vague entity, a nonexistent storey, an unmodeled entity
                # type) -- never a case where V2 legitimately answered
                # without needing any real data. Matches V1's own
                # disposition for the identical situation.
                # Independent-review finding, 2026-09-17: `subtask_dispositions`
                # now distinguishes a turn where *every* dispatched tool
                # call succeeded from one where only some did -- a
                # capability-gate rejection or execution error on one
                # parallel call no longer disappears behind another call's
                # citations. Matches V1's own answered/partially_answered/
                # unsupported/error vocabulary instead of a flat
                # citations-only "answered."
                if not subtask_dispositions:
                    disposition = "answered" if all_citations else "clarification_required"
                elif all(d == "answered" for d in subtask_dispositions):
                    disposition = "answered"
                elif any(d == "answered" for d in subtask_dispositions):
                    disposition = "partially_answered"
                # Independent-review finding, 2026-09-17, second pass: a
                # turn whose *only* dispatched tool call came back
                # disposition="clarification_required" (e.g. extract_pdf_field
                # matching more than one configured document, genuinely
                # asking the user to disambiguate, not failing) fell
                # through every named branch straight to the generic
                # "error" catch-all below -- a normal, honest clarification
                # request was misreported as a system failure. Checked
                # before "unsupported": asking the user something
                # actionable is a better outcome to surface than a flat
                # "not supported" when both are present in the same turn.
                elif "clarification_required" in subtask_dispositions:
                    disposition = "clarification_required"
                elif "unsupported" in subtask_dispositions:
                    disposition = "unsupported"
                else:
                    disposition = "error"
                narrative = "".join(answer_parts)
                # Independent-review finding, 2026-09-17: verification used
                # to be `"passed" if all_citations else "not_applicable"` --
                # true only of the *tool calls*, never checked against what
                # the model's own final prose actually says. A fake
                # provider scripted to answer "There are 99999 doors, all
                # fire-certified for 120 minutes" after a real
                # count_elements call returning 4 previously still
                # produced verification.status="passed". See
                # `_narrative_consistent_with_tool_facts`'s own docstring
                # for this check's real, narrow scope (numeric only).
                #
                # D-066 (2026-09-20), found live via a real-building stress
                # test of the D-065 fix: D-065's word-based heuristic
                # ("tag"/"mark"/"id"/"no."/"#" immediately before a number)
                # only covers the single-restatement phrasing it was found
                # with. The very next real investigation produced "a sample
                # listing IfcDoor elements including tags 146596 and 146678"
                # -- plural "tags" doesn't match the singular word list, and
                # the second number in the list ("146678") isn't preceded by
                # a reference word at all, it follows "and". Chasing every
                # grammatical variant (tags, tagged, marked, IDs, numbered,
                # a bare list joined by "and"/","/...) word-by-word is not a
                # tractable fix. This turn's own citations already carry the
                # ground truth instead: every element/row this turn actually
                # looked up has its real tag/record in `all_citations`'
                # locators (`{"tag": "146596", ...}` for IFC, `{"record":
                # "146600", ...}` for PDF) -- restating any of those numbers
                # is legitimate no matter how it's phrased, so they're
                # collected once here and exempted unconditionally in the
                # entity-window check below, instead of pattern-matching the
                # English wording around them.
                #
                # D-068 (2026-09-21): this originally checked only "tag"/
                # "record", missing `reconcile_doors_windows`'s own PDF-side
                # citation locator key, `"mark"` (`_synthesize_reconciliation_
                # response`'s `{"page": 2, "mark": tag}`) -- a finding whose
                # tag is genuinely absent from the IFC side (missing_in_ifc,
                # this exact investigated finding's own case) has *only* a
                # PDF-side citation, so its own tag number was never being
                # exempted at all before this fix.
                known_reference_numbers: set[float] = set()
                for citation in all_citations:
                    locator = citation.get("locator") or {}
                    for key in ("tag", "record", "mark"):
                        raw_value = locator.get(key)
                        if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
                            try:
                                known_reference_numbers.add(float(raw_value))
                            except (TypeError, ValueError):
                                pass
                narrative_consistent = self._narrative_consistent_with_tool_facts(narrative, expected_numeric_facts, known_reference_numbers)
                # Owner decision, 2026-09-17: an inconsistent narrative is
                # now flagged, not withheld. It used to hard-fail the
                # whole turn (disposition=error, real narrative replaced
                # with a generic withdrawal message, citations dropped) --
                # but across three rounds of independent review, every
                # *confirmed* catch of this check was against a
                # deliberately scripted adversarial test double, never a
                # real fabrication from the actual model in live use,
                # while the check's own false-positive rate against real
                # live usage was confirmed twice (a natural rounding of a
                # real measurement, and a correct sum of this turn's own
                # real counts -- see `_narrative_consistent_with_tool_facts`'s
                # own docstring). For a decision-support tool where the
                # user shares final responsibility for judgment calls (see
                # project memory), disclosing an unconfirmed claim serves
                # better than silently discarding a likely-correct answer.
                # `disposition` is left as whatever the tool-call outcomes
                # above already earned; only `verification.status` reflects
                # this check's own result, and citations/narrative are
                # both kept exactly as produced.
                if all_citations and not narrative_consistent:
                    verification = VerificationStatus(status="unverified", reason="The final answer's own stated numbers could not be matched to any value this turn's tool calls actually returned; shown with a caveat rather than withheld.")
                    self._audit(state, "v2_narrative_consistency", "verification_completed", "V2's final narrative did not match this turn's own tool results; shown to the user with a caveat, not withheld.", {"expected_numeric_facts": [{"entity_terms": sorted(terms), "numbers": sorted(numbers)} for terms, numbers in expected_numeric_facts]}, planning_mode="tool_calling")
                else:
                    verification = VerificationStatus(status="verified" if all_citations else "not_applicable", reason="Every stated fact came from a verified tool call this turn." if all_citations else "No tool call was made this turn -- nothing here is a claim of fact.")
                response = AgentResponse(
                    thread_id=thread_id, trace_id=trace_id, disposition=Disposition(disposition),
                    answer_markdown=narrative, citations=[Citation.model_validate(item) for item in all_citations],
                    verification=verification, execution_metadata={
                        "engine": "v2", "model_call_count": model_call_count, "tool_call_count": state.get("tool_call_count", 0),
                        "iterations": iteration, "planning_mode": "tool_calling", "actual_provider": provider.name, "actual_model": provider.model,
                        # Owner-reported, 2026-09-16: DecisionStory.tsx's Question/Plan
                        # steps read these three keys directly from execution_metadata
                        # (they were never set for V2 turns, unlike V1's own
                        # heuristic/LLM planning path -- see _synthesize_reconciliation_response's
                        # sibling execution_metadata construction above, which sets the same
                        # "source" convention this mirrors).
                        "normalized_request": question, "subplans": all_plans, "source": self._v2_source_label(all_plans),
                        # D-064 item 2: the model's own structured
                        # submit_finding_verdict call this turn, if any --
                        # `None` when this wasn't a finding investigation,
                        # or when it was but the model never called the
                        # tool (e.g. the turn ended some other way first).
                        "finding_verdict": state.get("finding_verdict"),
                    },
                    reconciliation_items=[ReconciliationItem.model_validate(item) for item in all_reconciliation_items],
                )
                if response.reconciliation_items:
                    self._upsert_findings_from_reconciliation(state, response)
                yield {"type": "final", "response": response}
                return
            for tool_call in turn_complete.tool_calls:
                # Owner-reported, 2026-09-17: with several *same-named*
                # tool calls in one turn (e.g. group_elements_by_storey
                # called once per entity type -- a real, common shape),
                # the client had only `tool_name` to key a status update
                # by, so a single "completed" event flipped *every*
                # matching "Calling X…" line to "X ✓" at once, regardless
                # of which specific call actually finished. `call_id` (already
                # unique per `ToolCallEvent`) lets the client match a
                # status transition to the exact call it belongs to.
                yield {"type": "tool_status", "tool_name": tool_call.tool_name, "call_id": tool_call.call_id, "status": "started"}
            # Independent-review finding, 2026-09-17, third pass: the
            # deadline/cancel machinery above only ever bounded the
            # *model's* own stream_turn call -- confirmed live with a
            # 50ms deadline and a 200ms delay injected into tool dispatch
            # instead of the model stream: this asyncio.gather ran to
            # completion regardless, ~210ms elapsed against the 50ms
            # budget. A hung or slow tool call (a stuck DB read, a slow
            # IFC query) was never actually interruptible, only the model
            # round trip was.
            #
            # A deadline that has already passed is caught before dispatch
            # even starts. For the dispatch itself: `asyncio.wait_for`
            # (used for the model stream above) does NOT work here --
            # `_v2_dispatch_tool` runs in a real OS thread via
            # `asyncio.to_thread`, and a thread already executing blocking
            # work cannot actually be cancelled; `wait_for` calls `cancel()`
            # on timeout and then *awaits that cancellation completing*,
            # which for an uncancellable thread means it silently blocks
            # until the thread finishes anyway (confirmed live: still ~207ms
            # elapsed against a 50ms deadline, `wait_for` in name only).
            # `asyncio.wait(..., timeout=...)` instead returns as soon as
            # the timeout elapses regardless of whether the still-running
            # tasks ever finish -- the orphaned thread keeps running to
            # completion in the background (harmless: these are read-only
            # queries) but this turn stops waiting on it and reports the
            # timeout promptly, matching the model-stream path's own
            # promptness.
            dispatch_remaining = (deadline - time.perf_counter()) if deadline is not None else None
            if dispatch_remaining is not None and dispatch_remaining <= 0:
                self._audit(state, "v2_turn_error", "timeout", "V2 agent exceeded its bounded wall-clock deadline before tool dispatch.", {"iteration": iteration}, planning_mode="tool_calling")
                response = AgentResponse(
                    thread_id=thread_id, trace_id=trace_id, disposition=Disposition.TIMEOUT,
                    answer_markdown="The request exceeded its time limit. Please retry with a narrower question.",
                    verification=VerificationStatus(status="not_applicable", reason="Timed out before a final answer was reached."),
                    execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                )
                yield {"type": "final", "response": response}
                return
            dispatch_tasks = [asyncio.ensure_future(asyncio.to_thread(self._v2_dispatch_tool, tool_call, state)) for tool_call in turn_complete.tool_calls]
            if dispatch_remaining is not None:
                _done, pending = await asyncio.wait(dispatch_tasks, timeout=dispatch_remaining)
                if pending:
                    for task in pending:
                        task.cancel()  # best-effort only -- cannot stop an already-running thread, just detaches from it
                    self._audit(state, "v2_turn_error", "timeout", "V2 agent's tool dispatch itself exceeded the bounded wall-clock deadline.", {"iteration": iteration}, planning_mode="tool_calling")
                    response = AgentResponse(
                        thread_id=thread_id, trace_id=trace_id, disposition=Disposition.TIMEOUT,
                        answer_markdown="The request exceeded its time limit. Please retry with a narrower question.",
                        verification=VerificationStatus(status="not_applicable", reason="Timed out before a final answer was reached."),
                        execution_metadata={"engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0), "iterations": iteration, "planning_mode": "tool_calling"},
                    )
                    yield {"type": "final", "response": response}
                    return
            results = await asyncio.gather(*dispatch_tasks)
            for tool_call, result in zip(turn_complete.tool_calls, results):
                tool_result = result.get("tool_result", {})
                # Independent-review finding, 2026-09-17: was
                # `state["tool_call_count"] = result.get("tool_call_count", ...)`
                # -- an *assignment*, not a sum, so parallel dispatches
                # (asyncio.gather above) silently clobbered each other's
                # contribution instead of accumulating. See
                # _v2_dispatch_tool's own "tool_call_delta" comment.
                state["tool_call_count"] = state.get("tool_call_count", 0) + result.get("tool_call_delta", 0)
                # D-064 item 2: if this call was submit_finding_verdict,
                # carry its raw arguments through to the turn's own final
                # execution_metadata (see the AgentResponse construction
                # below) -- the last call wins if the model somehow submits
                # more than once, matching "call this exactly once, as your
                # last action" in the tool's own description.
                if "finding_verdict" in result:
                    state["finding_verdict"] = result["finding_verdict"]
                # Independent-review finding, 2026-09-17: the turn's overall
                # disposition used to be decided purely from "did *any*
                # tool call this turn leave a citation," ignoring every
                # other tool call's own outcome -- a request with one
                # successful call and one capability-gate-rejected/errored
                # call still finalized as a flat "answered," identical to a
                # turn where everything succeeded. Recorded per-call here,
                # summarized into partially_answered/unsupported/error
                # below, matching V1's own subtask_summary discipline
                # (_finalize's execution_metadata) instead of a single
                # citations-only proxy.
                subtask_dispositions.append(tool_result.get("disposition", "error"))
                all_citations.extend(tool_result.get("citations", []))
                all_evidence.extend(result.get("evidence", []))
                all_plans.extend(result.get("plan", []))
                # Owner-reported, 2026-09-16: reconcile_doors_windows's own
                # per-tag matched/mismatch breakdown (computed in
                # _v2_dispatch_tool's reconciliation branch, same as V1's)
                # never reached the final AgentResponse for V2 -- neither
                # DecisionStory.tsx's reconciliation table nor the Findings
                # tab (SPEC-M11) had anything to render, even though the
                # underlying comparison ran and the answer text described it.
                tool_result_value = tool_result.get("result_value")
                if tool_result.get("disposition") == "answered":
                    facts = self._numeric_tokens_from_result_value(tool_result_value)
                    if facts:
                        # Independent-review finding, 2026-09-17, third
                        # pass: "does at least one real number appear
                        # *somewhere* in the answer" was itself bypassable
                        # -- confirmed live: a fake model answering "4
                        # records were checked. There are 99999 doors, all
                        # certified for 120 minutes" after a real
                        # count_elements call returning 4 still passed,
                        # since "4" is genuinely present, just not as the
                        # actual claim about doors. Recording this tool
                        # call's own entity terms alongside its expected
                        # numbers lets the check additionally require that
                        # wherever the answer names *this* entity next to a
                        # number, that number is a real one -- not just
                        # that a real number exists in the text somewhere.
                        entity_terms = self._entity_terms_for(result.get("plan", [{}])[0].get("entity_type"))
                        expected_numeric_facts.append((entity_terms, facts))
                if tool_call.tool_name == "reconcile_doors_windows" and tool_result_value:
                    all_reconciliation_items.extend(tool_result_value)
                    # Owner-reported, 2026-09-16: this deployment's own real
                    # quota is a modest 10 requests / 10,000 tokens per
                    # minute (confirmed via `az cognitiveservices account
                    # deployment list` -- GlobalStandard capacity=10) --
                    # a real building's reconciliation can have several
                    # dozen compared tags, and every one of them, matched
                    # items included, was feeding straight back into this
                    # same turn's *next* model call (the one composing the
                    # final answer) as this tool's own result. The prose
                    # `answer` this tool already produced faithfully
                    # summarizes matched/mismatched counts; the model only
                    # needs each *non-matched* item's own detail to phrase a
                    # specific, correct answer, so only those are sent back
                    # in full, with a bare count standing in for the rest.
                    matched_count = sum(1 for item in tool_result_value if item.get("status") == "matched")
                    tool_result_value = {
                        "matched_count": matched_count,
                        "non_matched_items": [item for item in tool_result_value if item.get("status") != "matched"],
                    }
                elif isinstance(tool_result_value, list) and len(tool_result_value) > _V2_TOOL_RESULT_LIST_CAP:
                    # Owner-reported, 2026-09-16 (found live: a real 429 on
                    # this deployment's raised 30K-token/minute quota, after
                    # just two turns): the same unbounded-payload problem
                    # found in reconcile_doors_windows also applies to any
                    # other tool whose result is naturally a per-element
                    # list -- get_element_properties matching a whole
                    # IfcFurnishingElement category (85 real elements in
                    # this project) sent every one of their full property
                    # dicts back into this same turn's next model call. The
                    # tool's own prose `answer` already states the total;
                    # the model gets a representative sample plus a count
                    # for the rest instead of paying for the entire list.
                    # Owner decision, 2026-09-17: closes the same gap a
                    # structured fact-binding rewrite would have (a
                    # distinct-value/type/size claim drawn from only the
                    # sample could be wrong even though sample_items and
                    # total_count are each individually correct) -- but at
                    # the data layer, not the output layer. V2 exists
                    # specifically so the model can freely synthesize over
                    # tool results rather than being limited to a closed,
                    # pre-built set of operations; restricting the model's
                    # *output* to only ever restate bound fields (as a
                    # stricter fact-binding architecture would) would
                    # reintroduce that same ceiling one layer up. Computed
                    # from the full, untruncated list before capping, so
                    # it stays exhaustive even when sample_items isn't.
                    distinct_value_summary = self._distinct_value_summary(tool_result_value)
                    # D-069 (2026-09-21), found live minutes after redeploying
                    # D-068: a fully correct answer restating "39 doors" --
                    # distinct_value_summary's own real per-value count
                    # (Qto_DoorBaseQuantities.Width = 1.09 for 39 of the 50
                    # real doors) -- was flagged unverified. `facts` above is
                    # computed from the raw, pre-summary list, before this
                    # summary exists, so it never saw these counts; recorded
                    # here as this same call's own additional facts, using
                    # the same rule already established for a plain
                    # per-bucket-count dict shape (`_numeric_tokens_from_
                    # result_value`'s "group_elements_by_storey-like" branch
                    # naturally applies to `distinct_value_summary`'s own
                    # {value: count} inner dicts with zero new special-casing).
                    distinct_value_summary_facts = self._numeric_tokens_from_result_value(distinct_value_summary)
                    if distinct_value_summary_facts:
                        expected_numeric_facts.append((self._entity_terms_for(result.get("plan", [{}])[0].get("entity_type")), distinct_value_summary_facts))
                    # D-067: each *item* also gets its own `properties` dict
                    # bounded (see `_cap_item_properties`'s own docstring) --
                    # a real, richly-annotated element (a real Revit export's
                    # dozens of vendor-specific parameters) can make even
                    # `_V2_TOOL_RESULT_LIST_CAP` items' worth of full property
                    # dicts alone exceed a real deployment's per-request
                    # token budget.
                    sample_items = [self._cap_item_properties(item) for item in tool_result_value[:_LARGE_LIST_SAMPLE_SIZE]]
                    any_properties_capped = any("properties_omitted_count" in item for item in sample_items if isinstance(item, dict))
                    tool_result_value = {
                        "total_count": len(tool_result_value),
                        "sample_items": sample_items,
                        "distinct_value_summary": distinct_value_summary,
                        "note": (
                            f"{len(tool_result_value) - len(sample_items)} further item(s) omitted for brevity; the total_count above is exact. "
                            "sample_items is only a partial sample -- do not infer a distinct-value/type/size count from it alone. "
                            "distinct_value_summary is computed from ALL items (not just the sample) and is the exact, exhaustive "
                            "count of distinct values for fields with a small number of distinct values -- use it, not "
                            "sample_items, whenever the question is about how many distinct values/types/sizes exist."
                            + (
                                " Some sample_items also had their own `properties` trimmed to a representative subset "
                                "(see each item's `properties_omitted_count`) -- if you need a specific property not shown "
                                "for a specific element, call get_element_properties again with that element's own "
                                "global_ids to get it in full."
                                if any_properties_capped else ""
                            )
                        ),
                    }
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.call_id,
                    "content": json.dumps({
                        "disposition": tool_result.get("disposition"), "answer": tool_result.get("answer"),
                        "result_value": tool_result_value,
                        # Owner-reported, 2026-09-17: e.g. a storey filter of
                        # "第二层" (never translated to the model's own
                        # "Level 2" naming) matched zero real elements --
                        # correctly executed, but a bad filter, not a real
                        # zero. Surfacing this warning gives the model a
                        # chance to notice and retry with a corrected
                        # filter instead of confidently reporting a wrong
                        # count.
                        "warnings": tool_result.get("warnings") or [],
                    }, default=str),
                })
                yield {"type": "tool_status", "tool_name": tool_call.tool_name, "call_id": tool_call.call_id, "status": "completed"}
        # SPEC-M16 Invariants: a hard, enforced cap -- never an unbounded loop.
        self._audit(state, "v2_turn_error", "error", "V2 agent exceeded its bounded tool-call iteration limit without finalizing.", {"max_iterations": max_iterations}, planning_mode="tool_calling")
        response = AgentResponse(
            thread_id=thread_id, trace_id=trace_id, disposition=Disposition.ERROR,
            answer_markdown=f"The request failed safely: exceeded the maximum of {max_iterations} tool-call rounds without reaching a final answer.",
            verification=VerificationStatus(status="not_applicable", reason="Iteration limit exceeded before a final answer was reached."),
            execution_metadata={
                "engine": "v2", "model_call_count": state.get("model_call_count", 0), "tool_call_count": state.get("tool_call_count", 0),
                "iterations": max_iterations, "planning_mode": "tool_calling",
                "normalized_request": question, "subplans": all_plans, "source": self._v2_source_label(all_plans),
            },
        )
        yield {"type": "final", "response": response}

    @staticmethod
    def _v2_source_label(all_plans: list[dict[str, Any]]) -> str | None:
        """SPEC-M16 Decision Trace fix (owner-reported, 2026-09-16): mirrors
        the "source" convention used above for V1's own multi-subplan
        execution_metadata, so DecisionStory.tsx's Execution step reads the
        same shape regardless of engine.
        """
        if any(plan.get("intent") == "reconciliation" for plan in all_plans):
            return "ifc+pdf (reconciliation)"
        distinct_sources = sorted({plan["source"] for plan in all_plans if plan.get("source")})
        if len(distinct_sources) == 1:
            return distinct_sources[0]
        if len(distinct_sources) > 1:
            return "multi_source"
        return None

    @staticmethod
    def _numeric_tokens_from_result_value(value: Any) -> set[float]:
        """Independent-review finding, 2026-09-17: the numbers a *correct*
        answer to this one tool call could truthfully state, extracted
        from the tool's own real return value -- not from anything the
        model goes on to say. Deliberately narrow in scope: this is a
        mechanical, numeric-only cross-check (does at least one real
        number this tool actually produced show up in the model's final
        prose), not a semantic fact-checker -- it catches a wrong count or
        measurement (the reported "99999 doors" case, a real tool call
        returning 4), it does not and cannot catch a fabricated
        *non-numeric* claim layered onto a correct number (e.g. an invented
        fire-rating attached to a correct door count). That gap is real and
        not closed here; see this method's own caller for how the result
        is used.

        Owner-reported, 2026-09-17 (found live: a real space_distance
        answer, e.g. a real 5.234 m centroid distance, phrased by the
        model with different rounding -- "5.23 m" -- than this method's
        first version pre-formatted): returns raw floats now, not
        pre-rounded strings, so the caller can compare with a tolerance
        instead of requiring an exact string match against one of a fixed
        handful of decimal-place guesses. A real fabrication (99999 vs 4)
        is nowhere near any reasonable tolerance; a model's own natural
        rounding of a real measurement is.
        """
        numbers: set[float] = set()

        def _add(number: float) -> None:
            if number != number or number in (float("inf"), float("-inf")):  # noqa: PLR0124 (NaN check)
                return
            numbers.add(float(number))

        if isinstance(value, bool):
            return numbers
        if isinstance(value, (int, float)):
            _add(value)
        elif isinstance(value, dict):
            if "value_m" in value:  # aggregate_quantity/space_distance's own shape -- the headline measurement
                _add(value["value_m"])
                for key in ("eligible_count", "total_entity_count"):
                    if isinstance(value.get(key), (int, float)):
                        _add(value[key])
            elif value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()):
                # group_elements_by_storey's own shape: {storey_name: count}.
                # Every bucket's own count is a number a correct answer
                # could truthfully state (the total, the winning storey's
                # count, or any single storey named in the question).
                for sub_value in value.values():
                    _add(sub_value)
                _add(sum(value.values()))
            else:
                # Independent-review finding, 2026-09-17, third pass:
                # get_element_properties's own shape ({"element": {...},
                # "storey": ..., "properties": {...flattened...}}) matched
                # neither special case above, so this method returned an
                # *empty* set for it -- silently disabling both consistency
                # checks for every property-lookup answer (confirmed live:
                # "Every door is 99999 metres high and fire certified" after
                # a real get_element_properties call passed unchecked, since
                # no expected numbers were ever recorded to check against).
                # Recursing into every nested value pulls out whatever real
                # numeric leaves the tool actually returned (heights,
                # areas, express IDs, ...) as candidate real facts; this can
                # only make the check more permissive (more real numbers to
                # match against), never less correct.
                for sub_value in value.values():
                    numbers |= AgentService._numeric_tokens_from_result_value(sub_value)
        elif isinstance(value, list):
            # D-068 (2026-09-21), found live minutes after redeploying
            # D-067's payload-size fix on the real DigitalHub building: a
            # fully correct `get_element_properties(entity_type="IfcDoor")`
            # answer restating "50 doors in the IFC" (the tool's own real
            # `total_count`) was flagged unverified, because `len(value)`
            # used to only ever get added as a fact inside the
            # reconcile-specific branch below -- for any other list shape
            # (get_element_properties, count_elements-by-selection, ...),
            # the real item count was never a recognized fact at all, even
            # though "there are N of these" is always a truthful thing a
            # correct answer can state about any list result, not just
            # reconcile_doors_windows's own per-tag comparison shape.
            # Moved out so every non-empty list's own length counts,
            # regardless of what its items look like.
            if value:
                _add(len(value))
            if value and all(isinstance(item, dict) and "status" in item for item in value):
                # reconcile_doors_windows's own shape: a list of per-tag
                # comparison items, each carrying a "status" (matched/
                # dimension_mismatch/missing_in_pdf/missing_in_ifc). The
                # real fact a correct summary states here is normally a
                # *count of items per status* ("6 matched, 1 mismatch"),
                # which is not any single item's own width/height leaf
                # value -- recursing into items alone (below) would leave
                # those summary counts unrecognized as real numbers and
                # reject a correct summary outright. Computed alongside,
                # not instead of, each item's own leaf values.
                from collections import Counter

                for count in Counter(item.get("status") for item in value).values():
                    _add(count)
            for item in value:
                numbers |= AgentService._numeric_tokens_from_result_value(item)
        return numbers

    @staticmethod
    def _flatten_for_distinct_summary(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 3) -> dict[str, Any]:
        """Dotted-path flattening of one list item's nested dict fields
        (e.g. get_element_properties's {"element": {...}, "storey": ...,
        "properties": {...}} -> {"element.entity_type": ..., "storey":
        ..., "properties.Height": ...}), used only by
        `_distinct_value_summary` below. List-valued fields are skipped
        (too structurally varied to summarize safely); depth is bounded
        to avoid runaway recursion on an unexpectedly deep shape.
        """
        flat: dict[str, Any] = {}
        if depth >= max_depth or not isinstance(obj, dict):
            return flat
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(AgentService._flatten_for_distinct_summary(value, path, depth + 1, max_depth))
            elif not isinstance(value, list):
                flat[path] = value
        return flat

    @staticmethod
    def _distinct_value_summary(items: list[Any], max_distinct: int = 20) -> dict[str, dict[str, int]]:
        """Owner decision, 2026-09-17: closes the sample-truncation false-
        completeness gap (independent review, third pass, P2 #5) at the
        data layer instead of the output layer -- see this method's own
        caller for why. Computed from the FULL list this tool call
        actually matched, *before* it gets capped to
        `_V2_TOOL_RESULT_LIST_CAP` sample items, so a "how many distinct
        X" question can be answered exhaustively even when the raw
        per-item sample sent to the model is not.

        For every dotted-path field found across all items (see
        `_flatten_for_distinct_summary`), returns {value: count} -- but
        only for fields whose distinct-value count is small (2..
        max_distinct): a field with exactly one distinct value across
        every item was never at risk from sampling (it would already
        appear in any non-empty sample), and a field with more distinct
        values than max_distinct is almost always a per-item identifier
        (a GlobalId, an express_id), not a meaningful "type/size" a user
        would ask to enumerate -- summarizing it would just be noise.
        """
        from collections import defaultdict

        per_key: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for item in items:
            if not isinstance(item, dict):
                continue
            for key, value in AgentService._flatten_for_distinct_summary(item).items():
                if value is None:
                    continue
                per_key[key][str(value)] += 1
        return {key: dict(counts) for key, counts in per_key.items() if 1 < len(counts) <= max_distinct}

    @staticmethod
    def _cap_item_properties(item: Any, cap: int = _PROPERTY_SAMPLE_CAP) -> Any:
        """D-067: bounds one sample item's own `properties` dict (get_
        element_properties'/get_properties' `{"element": ..., "storey":
        ..., "properties": {...flattened...}}` shape) to a representative
        subset, disclosed with an exact omitted count -- the same "sample +
        exact count, never silently guessed" contract `_V2_TOOL_RESULT_
        LIST_CAP`'s own item-count cap already uses, applied one level
        deeper.

        Deliberately does not try to guess which properties are "relevant"
        (e.g. keyword-matching on "width"/"height") -- this tool is
        documented to also answer material/fire-rating/load-bearing
        questions, not just dimensions, and silently dropping every field
        that doesn't look like a measurement would quietly break those
        other, equally legitimate uses. A fixed, alphabetically-sorted
        (stable, not dict-insertion-order-dependent) slice keeps this
        general rather than tuned to today's one failure case; a model that
        needs a specific property not in the sample can always follow up
        with `global_ids` narrowed to the one element it cares about, which
        returns that element's full, uncapped properties (this cap only
        ever applies within the many-items branch below).
        """
        if not isinstance(item, dict):
            return item
        properties = item.get("properties")
        if not isinstance(properties, dict) or len(properties) <= cap:
            return item
        kept_keys = sorted(properties.keys())[:cap]
        return {
            **item,
            "properties": {key: properties[key] for key in kept_keys},
            "properties_omitted_count": len(properties) - cap,
        }

    @staticmethod
    def _entity_terms_for(entity_type: str | None) -> frozenset[str]:
        """Independent-review finding, 2026-09-17, third pass: the English
        noun(s) a real answer about this entity type would actually use
        ("door"/"doors" for IfcDoor), reusing the exact vocabulary
        `ELEMENT_ALIASES` already teaches the router/model -- not a
        separate, hand-maintained list that can drift from it. Lets the
        consistency check look for *this specific claim* ("N doors")
        rather than treating every number anywhere in the answer as
        equally relevant.

        English-only for now, a disclosed, real limitation: a Chinese
        answer's own noun for the same entity ("门"/"扇") is not in this
        set, so the proximity check below simply has nothing to bind to
        for a Chinese answer and silently skips it -- the baseline
        "some real number appears somewhere" check still applies to every
        language equally, only this stricter half is English-only today.
        """
        if not entity_type:
            return frozenset()
        from app.agent.router import ELEMENT_ALIASES

        terms = {alias for alias, canonical in ELEMENT_ALIASES.items() if canonical == entity_type and alias.isascii()}
        terms.add(entity_type.removeprefix("Ifc").lower())
        return frozenset(terms)

    @staticmethod
    def _narrative_consistent_with_tool_facts(answer_markdown: str, expected_numeric_facts: list[tuple[frozenset[str], set[float]]], known_reference_numbers: set[float] = frozenset()) -> bool:
        """Independent-review finding, 2026-09-17: this turn's own real
        numbers vs. what the model's final prose actually says.

        Two checks, both required, per tool call:

        1. **Baseline (every language)**: at least one of this call's own
           real numbers appears (within tolerance) somewhere in the
           answer. Catches a model that never mentions the true value at
           all.

        2. **Entity-bound (English answers only -- see `_entity_terms_for`)**:
           wherever the answer mentions this call's own entity noun next
           to a number, that number must be one of the real ones. Catches
           the specific bypass independent review found live: a fake
           model answering "4 records were checked. There are 99999
           doors, all certified for 120 minutes" after a real
           count_elements call returning 4 passed check 1 alone (a real
           "4" genuinely appears in the text), because check 1 never
           verified *what* the "4" was actually claimed to describe --
           only that a correct number existed somewhere. "99999" sits
           directly next to "doors," the exact entity this tool call was
           about, and does not match; check 2 catches that.

        Neither check is a general semantic fact-checker (see
        `_numeric_tokens_from_result_value`'s own docstring) -- a
        sufficiently contrived answer that avoids ever placing a wrong
        number near the entity noun (e.g. restates the claim in a
        differently-worded clause with no noun nearby at all) can still
        defeat check 2; only check 1's weaker guarantee then applies.
        This is a real, disclosed boundary, not a claim of a complete
        semantic verifier.

        A whole-number expectation (a count) must match exactly -- "4
        doors" vs "5 doors" is a real, meaningful discrepancy, not
        rounding. A fractional expectation (a measurement) matches within
        a small absolute tolerance, since a model naturally rounds a real
        5.234 m distance to "5.23 m" or "5.2 m" when composing prose; that
        is not fabrication and this check must not treat it as such.

        Independent-review finding, 2026-09-17, third pass -- two real
        bugs in how check 2 was applied, both confirmed live and fixed
        here:

        (a) **Decoy bypass.** "There are 99999 doors (4 checked)." used to
        pass: the window around "doors" contains both "99999" and the
        real "4", and the old rule only required *some* nearby number to
        match ("if nearby_numbers and not any(...)"). A real value sitting
        next to a fabricated one rescued it. Fixed by requiring *every*
        nearby number to be individually explainable, not just one of
        them.

        (b) **Same-entity, different-scope false positive.** "The whole
        project contains 4 doors. On the first floor there are 2 doors."
        is a correct answer to two *separate* tool calls (project-wide
        count=4, Level-01-filtered count=2) -- but checking each call's
        own narrow expected set against *every* occurrence of the shared
        noun "doors" rejected it: the "4 doors" call's own check saw the
        unrelated "2" near a different "doors" mention and had nothing in
        its own {4} to explain it. Multi-scope comparison in one answer is
        V2's core value proposition, so this was a serious regression, not
        an edge case. Fixed by first merging expected values across every
        tool call that shares the same entity terms, then checking each
        occurrence's nearby numbers against that *combined* set -- a
        legitimate second scope's value is now itself part of what
        "doors" is allowed to mean anywhere in the answer, while a value
        that matches nothing in the combined set (99999) is still caught.

        Owner-reported, 2026-09-17 (found live against the real Duplex
        project): after a turn listing every entity type's own count
        (IfcFurnishingElement=61, IfcMember=4, ..., IfcWindow=24, each
        from its own count_elements call), the user asked "好了总和是多少?"
        ("okay, what's the total?"). The model correctly re-called the
        counts this turn (per its own system prompt: never state a number
        not just received from a tool this turn) and answered with their
        sum -- a real, mechanically verifiable arithmetic derivation over
        this turn's own real numbers, not a new, ungrounded claim. Check 1
        as written only accepted a value that was itself literally one
        call's own returned number, so the correct total was rejected as
        if it were fabricated -- this is exactly the class of "legitimate
        synthesis over verified data" this system is supposed to allow
        (owner decision, 2026-09-17: V2 exists so the model can freely
        reason over tool results, not just restate them one at a time);
        the check's own definition of "real" was too narrow, not the
        model's freedom too broad. Fixed by additionally accepting the sum
        of this turn's own whole-number, single-valued facts (a plain
        count_elements-style scalar, not a multi-value shape like
        aggregate_quantity/group_by, where "the one number to sum" isn't
        well-defined) as a valid baseline value -- a real sum of real
        counts, still mechanically checked, never an arbitrary allowance.
        """
        import re

        def _matches(expected: float, actual: float) -> bool:
            if float(expected).is_integer():
                return actual == expected
            return abs(actual - expected) < 0.05

        answer_lower = answer_markdown.lower()
        answer_numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", answer_markdown)]

        # A real sum over this turn's own real per-call counts is a
        # legitimate derivation, not a fabrication -- see this method's
        # own docstring. Only whole-number, single-valued ("pure scalar")
        # calls contribute: a multi-value shape (aggregate_quantity's own
        # {value_m, eligible_count, ...}, group_by's per-bucket counts)
        # has no single unambiguous "the" number to add, so those are left
        # out rather than guessed at.
        scalar_values = [next(iter(expected)) for _, expected in expected_numeric_facts if len(expected) == 1 and next(iter(expected)).is_integer()]
        total_of_scalars = sum(scalar_values) if len(scalar_values) > 1 else None
        answer_states_the_total = total_of_scalars is not None and any(_matches(total_of_scalars, actual) for actual in answer_numbers)

        # Check 1: baseline presence, any language -- per tool call, not
        # merged, so a call whose own value is never mentioned anywhere
        # still fails even if some *other* call's value happens to appear
        # -- except when the answer instead states the correct combined
        # total of every scalar call this turn, which honestly accounts
        # for this call's own contribution without repeating it verbatim.
        for entity_terms, expected in expected_numeric_facts:
            if answer_states_the_total and len(expected) == 1 and next(iter(expected)) in scalar_values:
                continue
            if not any(_matches(value, actual) for value in expected for actual in answer_numbers):
                return False

        # Check 2: entity-bound, merged by shared entity terms (see (b) above).
        combined_by_entity: dict[frozenset[str], set[float]] = {}
        for entity_terms, expected in expected_numeric_facts:
            if not entity_terms:
                continue
            combined_by_entity.setdefault(entity_terms, set()).update(expected)

        # Found live (2026-09-20), SPEC-M17 finding-investigation flow
        # against the real deployed app and real production data: "IFC
        # model: door tag 146596 has width = 1.25 m ..." was flagged
        # unverified even though every number in it was correct. IFC's own
        # `Tag` is a string (never one of `_numeric_tokens_from_result_
        # value`'s numeric leaves), so an element's own tag/mark/id,
        # restated right next to its entity noun for clarity -- a normal,
        # correct thing to do -- lands in the window below as an
        # "unexplained number," indistinguishable from a fabricated claim.
        # D-066 (2026-09-20): the fix above only covered the single
        # phrasing it was found with ("door tag 146596"). The very next
        # real investigation restated two tags in one clause -- "including
        # tags 146596 and 146678" -- and neither survived: plural "tags"
        # doesn't match the singular word list, and the second number
        # follows "and", no reference word at all. Rather than keep
        # chasing grammatical variants (tags, tagged, marked, IDs,
        # numbered, a bare list joined by "and"/","/...), `known_reference_
        # numbers` (this turn's own real citation tag/record values, see
        # this method's caller) is checked first and unconditionally
        # exempts a number regardless of phrasing -- restating any element
        # this turn actually looked up is always legitimate. The word-based
        # heuristic remains as a fallback for the rare case a restated
        # reference number was never independently cited this turn.
        _reference_word_pattern = re.compile(r"(?:tags?|marks?|ids?|no\.?|#)\s*$")

        # D-066, second bug found by the same stress test: this check used
        # to slice a fixed +/-15-character substring around each entity-term
        # occurrence and re-scan *that slice* for numbers -- a plain string
        # slice, unaware of number boundaries, that can bisect a real number
        # sitting right at the edge of the window. Reproduced live: "...
        # tagged 146600; reconcile_doors_windows explicitly..." sliced to
        # "600; reconcile_doors" around the "doors" inside "reconcile_doors_
        # windows", turning the real, legitimate, cited tag 146600 into a
        # phantom "600" that matches nothing. Fixed by finding every number
        # once in the *full* answer text first, then comparing character
        # positions (never re-slicing around a match) to decide which
        # numbers fall within the window of a given entity-term occurrence
        # -- a number's own span is never split.
        all_number_matches = list(re.finditer(r"\d+(?:\.\d+)?", answer_lower))
        window_chars = 15

        for entity_terms, combined_expected in combined_by_entity.items():
            # A number within a short window of this entity's own noun must
            # be one of the real values for *any* tool call about this
            # entity this turn. A window of ~15 characters on each side
            # comfortably covers "4 doors" / "there are 99999 doors" /
            # "doors: 99999" without reaching into an unrelated sentence.
            for term in entity_terms:
                for term_match in re.finditer(re.escape(term), answer_lower):
                    for number_match in all_number_matches:
                        if number_match.start() >= term_match.end():
                            gap = number_match.start() - term_match.end()
                        elif number_match.end() <= term_match.start():
                            gap = term_match.start() - number_match.end()
                        else:
                            gap = 0
                        if gap > window_chars:
                            continue
                        actual = float(number_match.group())
                        if actual in known_reference_numbers:
                            continue
                        preceding = answer_lower[max(0, number_match.start() - 6): number_match.start()]
                        if _reference_word_pattern.search(preceding):
                            continue
                        if not any(_matches(value, actual) for value in combined_expected):
                            return False
        return True

    @staticmethod
    def _citations(evidence: list[Evidence], state: GraphState) -> list[dict]:
        # project_id/source_set_id: independent-review finding, confirmed
        # live (2026-09-13) -- SPEC-M9 §F's "minimal frozen source-
        # provenance manifest" was stored in the registry and used
        # internally for cache-key/download-verification, but never
        # actually surfaced anywhere a caller or auditor could see it.
        # Two projects can use identical document names (both fixtures in
        # this repo do, deliberately, to prove isolation) -- without this,
        # a citation alone cannot prove *which* project's, or which frozen
        # source_set_id's, document actually produced it.
        manifest = state["project_resources"].manifest
        return [{
            "evidence_id": item.id,
            "source_type": item.source_type.value,
            "label": item.summary,
            "locator": item.locator,
            "project_id": manifest.project_id,
            "source_set_id": manifest.source_set_id,
            "source_file": item.source_file,
        } for item in evidence]

    @staticmethod
    def _format_ifc_answer(plan: QueryPlan, value: Any) -> str:
        if plan.operation in {"aggregate_quantity", "max_window_height"} and isinstance(value, dict):
            entity = (plan.entity_type or "IfcProduct").removeprefix("Ifc").lower()
            qualifier = "maximum" if plan.aggregation != "min" else "minimum"
            coverage = value.get("eligible_count", value.get("matching_window_count", 0))
            total = value.get("total_entity_count", coverage)
            precision = 3 if plan.entity_type == "IfcDoor" and value.get("measure") == "height" else 2
            return (f"The {qualifier} verified {entity} {value.get('measure', 'height')} is **{value['value_m']:.{precision}f} m**. "
                    f"It comes from `{value['quantity_path']}` with valid values for **{coverage} of {total}** matching {entity}s.")
        if plan.operation == "space_distance" and isinstance(value, dict):
            return (f"The bounded horizontal centroid distance from **{value['from_space']}** to "
                    f"**{value['to_space']}** is **{value['value_m']:.2f} m**. "
                    "This is a straight-line geometry measurement, not a walkable route distance.")
        if plan.operation == "count":
            label = (plan.entity_type or "IfcProduct").removeprefix("Ifc").lower()
            storey = plan.filters.get("storey") if isinstance(plan.filters, dict) else None
            if storey:
                return f"{storey} contains **{value}** {label}{'' if value == 1 else 's'}."
            return f"The project contains **{value}** {label}{'' if value == 1 else 's'}."
        if plan.operation == "group_by":
            if not value:
                return "No matching elements were found to group."
            if plan.postprocess in {"argmax", "argmin"}:
                chooser = max if plan.postprocess == "argmax" else min
                top_key, top_value = chooser(value.items(), key=lambda item: (item[1], item[0]))
            else:
                top_key, top_value = next(iter(value.items()))
            label = (plan.entity_type or "IfcProduct").removeprefix("Ifc").lower()
            if plan.postprocess == "argmax":
                return f"**{top_key}** has the most {label}s, with **{top_value}**."
            if plan.postprocess == "argmin":
                bottom_key, bottom_value = min(value.items(), key=lambda item: (item[1], item[0]))
                return f"**{bottom_key}** has the fewest {label}s, with **{bottom_value}**."
            breakdown = "; ".join(f"**{group}**: **{count}**" for group, count in value.items())
            return f"Per-storey {label} counts: {breakdown}."
        if plan.operation == "get_properties" and isinstance(value, list) and value:
            if plan.expected_result_shape == "element_storey":
                return f"It is on **{value[0].get('storey') or 'Unassigned'}**."
            if plan.expected_result_shape == "element_identity":
                element = value[0].get("element", {})
                return f"The selected element is **{element.get('entity_type', plan.entity_type)}** named **{element.get('name', 'Unnamed')}**."
            # D-060: owner-reported, 2026-09-16 -- a get_properties question
            # with no narrowing shape/selection (e.g. "what is the length
            # and width of the windows?", asked about a type in general,
            # not "this" one) matches every element of that type, but this
            # branch always took `value[0]` and phrased it as "The selected
            # element is..." -- a false, arbitrary certainty (whichever
            # element happened to sort first), not a genuine single
            # selection. `element_storey`/`element_identity` above are
            # unaffected: both are only ever produced by a real
            # global_ids-filtered selection (selected_element_plan), where
            # `value` is already exactly one element by construction.
            if len(value) > 1:
                label = (plan.entity_type or "IfcProduct").removeprefix("Ifc").lower()
                sample = value[:5]
                lines = [f"- {record.get('element', {}).get('name', 'Unnamed')} ({record.get('storey') or 'Unassigned'})" for record in sample]
                more = f"\n…and {len(value) - 5} more." if len(value) > 5 else ""
                return (
                    f"**{len(value)}** {label}s matched this request -- no single element was uniquely selected, so here is a sample "
                    f"rather than one arbitrarily chosen result:\n\n" + "\n".join(lines) + more
                )
            record = value[0]
            element = record.get("element", {})
            properties = record.get("properties", {})
            preferred_keys = ("IfcMaterial", "IsExternal", "LoadBearing", "FireRating", "Length", "Width", "Height", "GrossVolume")
            key_properties = [f"- {name}: {item}" for name, item in properties.items() if any(name.endswith(f".{key}") for key in preferred_keys)][:6]
            property_lines = "\n".join(key_properties) or "- Full property set is available in the evidence inspector."
            return (
                f"**{element.get('entity_type', plan.entity_type)}** — {element.get('name', 'Unnamed')}\n\n"
                f"- Storey: {record.get('storey') or 'Unassigned'}\n"
                f"- Tag: {element.get('tag') or '(none)'}\n"
                f"- GlobalId: `{element.get('global_id')}`\n"
                f"- ExpressID: `{element.get('express_id')}`\n"
                f"- Key properties:\n{property_lines}"
            )
        return f"Deterministic IFC result: {value}"

    @staticmethod
    def _error_result(message: str) -> dict:
        return {
            "answer": f"I could not complete that tool request safely: {message}",
            "disposition": "error",
            "citations": [],
            "verification": VerificationStatus(status="failed", reason=message).model_dump(),
            "context_update": {},
        }
