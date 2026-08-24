from __future__ import annotations

import asyncio
from typing import Any, Optional, TypedDict
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
    capability_gate,
    cross_source_join_requested,
    fast_path_coverage,
    ground_plan_to_selection,
    heuristic_multi_plan,
    heuristic_plan,
    nearest_space_requested,
    planner_prompt,
    resolve_reference,
    selected_element_plan,
)
from app.config import Settings
from app.schemas.models import (
    AgentResponse,
    AuditEvent,
    Citation,
    ConversationDelta,
    Disposition,
    DocumentQueryInput,
    Evidence,
    IfcQueryInput,
    MultiQueryPlan,
    QueryPlan,
    ResponseLanguage,
    SourceType,
    VerificationStatus,
    VerifierResult,
)
from app.schemas.vision import VisionViewerInspection, VisionViewerVerification
from app.services import ServiceContainer
from app.verification.verifiers import (
    DeterministicVerifier,
    EvidenceVerifier,
    InvariantValidator,
    verification_status,
)


class GraphState(TypedDict, total=False):
    thread_id: str
    trace_id: str
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
                resolved_selection = self.container.ifc_repository.resolve_selection(
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
                        declared_entities = {
                            "door": "IfcDoor", "window": "IfcWindow", "wall": "IfcWall",
                            "space": "IfcSpace", "room": "IfcSpace", "stair": "IfcStair", "slab": "IfcSlab",
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
                batch_values = self.container.ifc_repository.execute_batch_counts(entity_types, batchable[0].filters)
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
                subresults.append(self._unsupported_subresult(subtask_id, plan, reason or "Unsupported subplan."))
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
        result = self.container.ifc_repository.execute(query)
        # The independent tool query is intentionally used for evidence and
        # verification; the batch value is checked so a batch optimisation never
        # weakens deterministic assurance.
        if result.value != value:
            return {"tool_result": self._error_result("Batch count disagreed with independent IFC verification."), "tool_call_count": state.get("tool_call_count", 0) + 1}
        citations = self._citations(result.evidence)
        answer = self._format_ifc_answer(plan, value)
        consistency_issues = verify_execution_consistency(plan, tool_query=query.model_dump(), result_value=value, answer=answer)
        self._audit(state, "execution_consistency", "execution_consistency", "Scalar batch execution consistency checked.", {"status": "passed" if not consistency_issues else "failed", "issues": [issue.__dict__ for issue in consistency_issues], "result_shape": "scalar_count"})
        consistency = VerifierResult(verifier="intent_execution_consistency", passed=not consistency_issues, reason="Execution preserved the validated scalar count contract." if not consistency_issues else consistency_issues[0].message)
        status = verification_status([
            DeterministicVerifier(self.container.ifc_repository).verify(query, result),
            InvariantValidator().validate(evidence=result.evidence, citations=[Citation.model_validate(item) for item in citations], disposition="answered"),
            consistency,
        ])
        return {"tool_result": {
            "answer": answer, "disposition": "answered" if status.status == "passed" else "error", "citations": citations,
            "verification": status.model_dump(),
            "result_value": value,
            "context_update": {"active_source": SourceType.IFC.value, "active_entity_type": plan.entity_type, "active_filters": plan.filters, "previous_query_plan": plan.model_dump(), "evidence_refs": [item.id for item in result.evidence]},
        }, "evidence": [item.model_dump() for item in result.evidence], "tool_call_count": state.get("tool_call_count", 0) + 1}

    def _unsupported_subresult(self, subtask_id: str, plan: QueryPlan, reason: str) -> dict:
        return {"subtask_id": subtask_id, "plan": plan.model_dump(), "answer": reason, "disposition": "refused", "citations": [], "verification": VerificationStatus(status="not_applicable", reason=reason).model_dump(), "context_update": {}}

    def _synthesize_multi_response(self, state: GraphState, multi_plan: MultiQueryPlan, subresults: list[dict[str, Any]], model_calls: int) -> dict:
        if cross_source_join_requested(state.get("question", "")):
            reason = "Cross-source joins between drawing and IFC room-area data are outside this reference implementation. No numeric partial answer can be finalized."
            self._audit(state, "intent_coverage", "capability_gate_rejected", "Whole-intent coverage rejected a partial cross-source execution.", {"reason": "cross_source_join_unsupported"})
            return {"answer": reason, "disposition": "refused", "citations": [], "verification": VerificationStatus(status="not_applicable", reason=reason).model_dump(), "model_call_count": model_calls}
        successful = [item for item in subresults if item.get("disposition") == "answered" and item.get("verification", {}).get("status") == "passed"]
        unresolved = [item for item in subresults if item not in successful]
        citations = [citation for item in subresults for citation in item.get("citations", [])]
        answer = self._natural_answer(multi_plan, subresults)
        disposition = "answered" if successful and not unresolved else ("partially_answered" if successful else subresults[0].get("disposition", "refused"))
        verification = VerificationStatus(status="passed" if disposition == "answered" else "not_applicable", reason=None if disposition == "answered" else "One or more subtasks were unresolved; successful subtasks remain independently verified.")
        self._audit(state, "synthesize", "synthesized", "Subplan outcomes synthesized without omitting partial results.", {"successful_subtasks": len(successful), "unresolved_subtasks": len(unresolved), "disposition": disposition})
        return {"answer": answer, "disposition": disposition, "citations": citations, "verification": verification.model_dump(), "model_call_count": model_calls}

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
                    noun = {"IfcDoor": "门", "IfcWindow": "窗", "IfcWall": "墙"}.get(plan.get("entity_type"), "构件")
                    if chinese and value:
                        fragments.append(f"这个项目中共有 **{value.group(1)}** 扇{noun}。")
                    else:
                        fragments.append(answer)
                elif plan.get("source") == "ifc" and plan.get("operation") == "group_by" and chinese:
                    noun = {"IfcWindow": "窗户", "IfcDoor": "门"}.get(plan.get("entity_type"), "构件")
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
                    record = records[0] if isinstance(records, list) and records else {}
                    element = record.get("element", {}) if isinstance(record, dict) else {}
                    label = {"IfcDoor": "门", "IfcWindow": "窗", "IfcWall": "墙", "IfcSpace": "空间", "IfcStair": "楼梯"}.get(element.get("entity_type"), "构件")
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
            result = self.container.ifc_repository.execute(query)
            citations = self._citations(result.evidence)
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
                DeterministicVerifier(self.container.ifc_repository).verify(query, result),
                InvariantValidator().validate(
                    evidence=result.evidence,
                    citations=[Citation.model_validate(item) for item in citations],
                    disposition="answered",
                ),
                consistency,
            ])
            tool_result = {
                "answer": answer if status.status == "passed" else "I could not safely finalize this answer because the execution result did not preserve the requested operation.",
                "disposition": "answered" if status.status == "passed" else "error",
                "citations": citations,
                "verification": status.model_dump(),
                "result_value": result.value,
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

    def _execute_pdf(self, state: GraphState) -> dict:
        plan = QueryPlan.model_validate(state["plan"])
        query = DocumentQueryInput(
            field=plan.requested_field or state["question"],
            question=state["question"],
        )
        self._audit(state, "execute_pdf", "tool_called", "PDF document tool invoked.", {"query": query.model_dump()})
        try:
            tool_call_count = state.get("tool_call_count", 0) + 1
            model_call_count = state.get("model_call_count", 0)
            result = self.container.document_analyzer.native_lookup(query)
            self._audit(
                state,
                "execute_pdf",
                "tool_completed",
                "Deterministic native-text extraction attempted.",
                {"confidence": result.confidence, "extraction_method": result.extraction_method, "ambiguity": result.ambiguity},
            )

            if result.confidence < self.settings.pdf_confidence_threshold:
                board = self.container.document_analyzer.target_board(query.question)
                field = self.container.document_analyzer.target_field(query.question)
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
                    location, _ = asyncio.run(self.container.document_analyzer.localize_board(provider, query, board))
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
                    candidates, crop = asyncio.run(self.container.document_analyzer.board_candidates(provider, query, board, location.bbox))
                    model_call_count += 1
                    normalized = [
                        candidate for candidate in candidates.candidates
                        if (candidate.board or board).upper() == board
                        and self.container.document_analyzer.canonical_field(candidate.field)
                        == self.container.document_analyzer.canonical_field(field)
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
                    verification = asyncio.run(self.container.document_analyzer.verify_board_candidate(provider, query, board, candidate, crop))
                    model_call_count += 1
                    self._audit(state, "execute_pdf", "model_completed", "Independent same-crop PDF verification completed.", {"verification": verification.model_dump()}, actual_provider=provider.name, actual_model=provider.model)
                    evidence = [Evidence(
                        source_type=SourceType.PDF, source_file=self.settings.pdf_file,
                        summary=f"{board} · {field}: {candidate.value}{(' ' + candidate.unit) if candidate.unit else ''}",
                        locator={"page": query.page_hint or 1, "bbox": location.bbox, "field": field, "board": board, "extraction_method": "board_localized_vision", "evidence_crop": crop.name},
                        extracted_value=candidate.value, confidence=candidate.confidence,
                    )]
                    citations = self._citations(evidence)
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
                    self.container.document_analyzer.vision_lookup(provider, query)
                )
                model_call_count += 1
                self._audit(state, "execute_pdf", "model_completed", "PDF vision extraction completed.", {"value": result.value, "bbox": result.bbox, "ambiguity": result.ambiguity}, actual_provider=provider.name, actual_model=provider.model)
                self._audit(state, "execute_pdf", "model_called", "Independent PDF evidence verifier invoked.", {"purpose": "pdf_verify"}, actual_provider=provider.name, actual_model=provider.model)
                visual_verification = asyncio.run(
                    self.container.document_analyzer.verify_vision_extraction(provider, query, result)
                )
                model_call_count += 1
                self._audit(state, "execute_pdf", "model_completed", "Independent PDF evidence verification completed.", {"supported": visual_verification.supported, "confidence": visual_verification.confidence, "rationale": visual_verification.rationale}, actual_provider=provider.name, actual_model=provider.model)
                if not visual_verification.supported:
                    result.confidence = min(result.confidence, visual_verification.confidence)
                    result.ambiguity = visual_verification.rationale
            citations = self._citations(result.evidence)
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
                    source_file=self.settings.pdf_file,
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
                    "citations": self._citations(evidence),
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
            source_file=self.settings.ifc_file,
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
                "disposition": "clarification_required", "citations": self._citations(evidence),
                "verification": VerificationStatus(status="not_applicable", reason=viewer_verification.rationale).model_dump(),
                "context_update": {"active_source": SourceType.VIEWER.value, "active_snapshot_id": viewer.get("snapshot_id")},
            }, "evidence": [item.model_dump() for item in evidence], "model_call_count": model_call_count}
        return {"tool_result": {
            "answer": claim,
            "disposition": "answered",
            "citations": self._citations(evidence),
            "verification": VerificationStatus(status="passed", verifier_results=[VerifierResult(verifier="same_snapshot_vision", passed=True, confidence=viewer_verification.confidence, reason=viewer_verification.rationale, supporting_evidence_ids=[evidence[0].id])], reason="Vision result was independently verified against the supplied snapshot.").model_dump(),
            "context_update": {"active_source": SourceType.VIEWER.value, "active_snapshot_id": viewer.get("snapshot_id")},
        }, "evidence": [item.model_dump() for item in evidence], "model_call_count": model_call_count}

    def _refuse(self, state: GraphState) -> dict:
        self._audit(state, "refuse", "refused", "No safe supported tool route.", {"plan": state.get("plan")})
        return {"tool_result": {
            "answer": state.get("planner_error") or state.get("unsupported_reason") or state.get("plan", {}).get("rationale") or "I cannot answer that reliably from the configured IFC, drawing, or current viewer evidence. Please ask a question about a model element, a drawing field, or the visible selected view.",
            "disposition": "error" if state.get("planner_error") else "refused",
            "citations": [],
            "verification": VerificationStatus(status="not_applicable").model_dump(),
            "context_update": {},
        }}

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
            response = AgentResponse(
                thread_id=state["thread_id"],
                trace_id=state["trace_id"],
                disposition=Disposition(result.get("disposition", "answered")),
                answer_markdown=result["answer"],
                citations=[Citation.model_validate(item) for item in result.get("citations", [])],
                verification=VerificationStatus.model_validate(result["verification"]),
                execution_metadata={
                    "source": state.get("plan", {}).get("source") if len(state.get("multi_plan", {}).get("subplans", [])) <= 1 else "multi_source",
                    "planning_mode": "heuristic" if all(item.get("planning_mode") == "heuristic" for item in state.get("multi_plan", {}).get("subplans", [])) else "llm",
                    "configured_provider": self.settings.llm_provider,
                    "model_call_count": state.get("model_call_count", 0),
                    "tool_call_count": state.get("tool_call_count", 0),
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
            )
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
        thread_id: str | None,
        question: str,
        viewer_context: dict | None,
        conversation_context: dict | None = None,
        source_preference: str = "auto",
    ) -> AgentResponse:
        initial: GraphState = {
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
    def _citations(evidence: list[Evidence]) -> list[dict]:
        return [{
            "evidence_id": item.id,
            "source_type": item.source_type.value,
            "label": item.summary,
            "locator": item.locator,
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
            record = value[0]
            element = record.get("element", {})
            properties = record.get("properties", {})
            if plan.expected_result_shape == "element_storey":
                return f"It is on **{record.get('storey') or 'Unassigned'}**."
            if plan.expected_result_shape == "element_identity":
                return f"The selected element is **{element.get('entity_type', plan.entity_type)}** named **{element.get('name', 'Unnamed')}**."
            preferred_keys = ("IfcMaterial", "IsExternal", "LoadBearing", "FireRating", "Length", "Width", "Height", "GrossVolume")
            key_properties = [f"- {name}: {item}" for name, item in properties.items() if any(name.endswith(f".{key}") for key in preferred_keys)][:6]
            property_lines = "\n".join(key_properties) or "- Full property set is available in the evidence inspector."
            return (
                f"**{element.get('entity_type', plan.entity_type)}** — {element.get('name', 'Unnamed')}\n\n"
                f"- Storey: {record.get('storey') or 'Unassigned'}\n"
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
