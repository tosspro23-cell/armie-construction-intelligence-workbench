"""Failure-path evals F1-F12 (SPEC-M1 §4.5), driven entirely by FakeModelProvider.

Each test asserts the disposition required by the canonical mapping in
SPEC-M1 §4.3 *and* the absence of an unverified/fabricated numeric or
factual claim. Where production behaviour genuinely diverges from the
normative table, the test asserts the actual (still-safe) behaviour and
documents the divergence as a defect finding -- per §4.3 ("any
implementation divergence is a defect finding, not a test to adjust") --
rather than silently asserting the wrong thing or leaving the suite red.
See the PR "Defect findings" section for the consolidated writeup.

No network I/O, no Ollama, no OpenAI key required.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.providers.ollama_provider import StructuredOutputError, _extract_json
from app.schemas.models import ChatRequest, MultiQueryPlan, QueryPlan, ResponseLanguage
from app.schemas.vision import VisionEvidenceVerification, VisionFieldExtraction
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider, sleep_past_deadline

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf",
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _service(settings: Settings, fake: FakeModelProvider, *, escalation_factory=None) -> tuple[ServiceContainer, AgentService]:
    kwargs = dict(text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    if escalation_factory is not None:
        kwargs["escalation_provider_factory"] = escalation_factory
    container = ServiceContainer(settings, **kwargs)
    return container, AgentService(container)


def _count_plan(entity_type: str, **overrides) -> MultiQueryPlan:
    defaults = dict(subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type=entity_type, filters={}, group_by="none", expected_result_shape="scalar_count", rationale="test plan", planning_mode="llm", match_status="complete")
    defaults.update(overrides)
    return MultiQueryPlan(response_language="en", rationale="test", subplans=[QueryPlan(**defaults)])


QUESTION = "Please describe the situation with the doors in this project."


# --- F1: malformed output, bounded repair succeeds --------------------------------

def test_f1_malformed_output_repair_succeeds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", StructuredOutputError("The model returned non-JSON output."))
    fake.script("multi_query_plan_repair", _count_plan("IfcDoor"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f1", question=QUESTION, viewer_context=None)

    assert response.disposition.value == "answered"
    assert response.verification.status == "passed"
    trace = container.audit_store.by_trace(response.trace_id)
    repair_events = [event for event in trace if event.step == "semantic_repair" and event.event_type == "semantic_repair"]
    assert any("repair completed" in event.summary.lower() for event in repair_events)


# --- F2: malformed output, repair also fails ---------------------------------------

def test_f2_malformed_output_repair_also_fails(tmp_path: Path) -> None:
    """SPEC-M1.5 §4A: production graph.py._route sets state["planner_error"]
    on this path; _execute_multi/_unsupported_subresult now consults it
    before falling back to plan.intent, so this correctly surfaces as
    disposition="error" per §4.3, instead of collapsing into "refused" as it
    did through M2P1 (see the former DEFECT FINDING note, now resolved --
    D-010).
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", StructuredOutputError("bad json"))
    fake.script("multi_query_plan_repair", StructuredOutputError("still bad json"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f2", question=QUESTION, viewer_context=None)

    assert response.disposition.value == "error"
    assert response.verification.status != "passed"
    assert not any(char.isdigit() for char in response.answer_markdown)


# --- F3: fenced-JSON and empty-string outputs (pure _extract_json contract) -------

def test_f3_extract_json_unwraps_fenced_code_block() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_f3_extract_json_unwraps_unlabeled_fence() -> None:
    assert _extract_json('```\n{"a": 1}\n```') == '{"a": 1}'


@pytest.mark.parametrize("content", ["", None])
def test_f3_extract_json_raises_on_empty_content(content) -> None:
    with pytest.raises(StructuredOutputError):
        _extract_json(content)


def test_f3_extract_json_raises_rather_than_silently_coercing_non_json() -> None:
    with pytest.raises(StructuredOutputError):
        _extract_json("The answer is probably 4 doors.")


# --- F4: schema-valid but semantically wrong plan -----------------------------------

def test_f4_semantically_wrong_plan_caught_by_validation_and_repaired(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    # postprocess=argmax without a grouping dimension: schema-valid, but
    # validate_multi_plan's comparison_requires_grouping check must catch it.
    bad_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="aggregate", operation="count", entity_type="IfcWindow",
        filters={}, group_by="none", postprocess="argmax", expected_result_shape="scalar_count",
        rationale="bad", planning_mode="llm", match_status="complete",
    )])
    fixed_plan = MultiQueryPlan(response_language="en", rationale="r2", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="aggregate", operation="group_by", entity_type="IfcWindow",
        filters={}, group_by="storey", postprocess="argmax", expected_result_shape="single_group_extremum",
        rationale="fixed", planning_mode="llm", match_status="complete",
    )])
    fake.script("multi_query_plan", bad_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    fake.script("multi_query_plan_repair", fixed_plan)
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f4", question="Please describe the situation with the windows in this project.", viewer_context=None)

    assert response.disposition.value == "answered"
    trace = container.audit_store.by_trace(response.trace_id)
    validation_events = [event for event in trace if event.step == "semantic_validate"]
    assert any(event.payload.get("status") == "failed" for event in validation_events)
    assert any(event.payload.get("status") == "passed" for event in validation_events)
    repair_events = [event for event in trace if event.step == "semantic_repair"]
    assert repair_events


# --- F5: persistent validation failure escalates, escalation unavailable ------------

def test_f5_persistent_failure_escalates_then_clarifies_when_escalation_unavailable(tmp_path: Path) -> None:
    """SPEC-M1.5 §4A: the "clarification" intent set on the MultiQueryPlan at
    graph.py's issues-still-present branch produces a subplan with
    source="unsupported" and intent="clarification"; _unsupported_subresult
    now maps that intent to disposition="clarification_required" per §4.3,
    instead of collapsing into "refused" as it did through M2P1 (D-010).
    """
    settings = _settings(tmp_path, ollama_escalation_model="qwen3:30b-test")
    bad_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="aggregate", operation="count", entity_type="IfcWindow",
        filters={}, group_by="none", postprocess="argmax", expected_result_shape="scalar_count",
        rationale="bad", planning_mode="llm", match_status="complete",
    )])
    still_bad_plan = bad_plan.model_copy(update={"rationale": "still bad"})
    fake = FakeModelProvider()
    fake.script("multi_query_plan", bad_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    fake.script("multi_query_plan_repair", still_bad_plan)

    escalation_fake = FakeModelProvider(name="ollama", model="qwen3:30b-test")
    escalation_fake.script("multi_query_plan_escalation", ConnectionError("escalation model unavailable"))

    container, service = _service(settings, fake, escalation_factory=lambda s: escalation_fake)

    response = service.invoke(thread_id="f5", question="Please describe the situation with the windows in this project.", viewer_context=None)

    assert response.disposition.value == "clarification_required"
    assert not any(char.isdigit() for char in response.answer_markdown)
    trace = container.audit_store.by_trace(response.trace_id)
    escalation_events = [event for event in trace if event.event_type in {"model_escalation", "model_failed"} and event.retry_count == 2]
    assert len(escalation_events) == 2
    assert all(event.actual_model == "qwen3:30b-test" for event in escalation_events)


def test_f5_escalation_skipped_when_not_configured(tmp_path: Path) -> None:
    """Opt-in escalation (OD-2): with no ollama_escalation_model configured
    (the default), escalation must be skipped -- audited as not configured --
    without any network call, never silently requiring a 30B model.
    """
    settings = _settings(tmp_path)  # ollama_escalation_model defaults to None
    bad_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="aggregate", operation="count", entity_type="IfcWindow",
        filters={}, group_by="none", postprocess="argmax", expected_result_shape="scalar_count",
        rationale="bad", planning_mode="llm", match_status="complete",
    )])
    fake = FakeModelProvider()
    fake.script("multi_query_plan", bad_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    fake.script("multi_query_plan_repair", bad_plan.model_copy(update={"rationale": "still bad"}))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f5b", question="Please describe the situation with the windows in this project.", viewer_context=None)

    trace = container.audit_store.by_trace(response.trace_id)
    not_configured = [event for event in trace if event.event_type == "model_failed" and "not configured" in event.summary.lower()]
    assert len(not_configured) == 1
    assert not_configured[0].actual_provider is None


# --- F6: provider raises a transport error ------------------------------------------

def test_f6_transport_error_produces_a_safe_non_answer(tmp_path: Path) -> None:
    """SPEC-M1.5 §4A: same root cause as F2 -- a transport error is caught by
    the same generic except-and-repair block, and when repair also fails,
    planner_error is set on state. _unsupported_subresult now consults it,
    so this correctly surfaces as disposition="error", distinct from
    unsupported/clarification, instead of collapsing into "refused" as it
    did through M2P1 (D-010).
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", ConnectionError("connection refused"))
    fake.script("multi_query_plan_repair", ConnectionError("connection refused"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f6", question=QUESTION, viewer_context=None)

    assert response.disposition.value == "error"
    assert response.verification.status != "passed"
    assert not any(char.isdigit() for char in response.answer_markdown)


# --- F7: provider stalls past the configured deadline -------------------------------

def test_f7_stalled_provider_produces_a_distinct_timeout_disposition(tmp_path: Path) -> None:
    """Exercised at the request boundary (main.chat's asyncio.wait_for around
    agent.invoke), which is the mechanism that actually produces a timeout
    disposition distinct from error in this codebase -- graph.py itself has
    no per-model-call timeout branch; the provider's own transport timeout
    is otherwise indistinguishable from any other transport error (F6).
    """
    import app.main as main_module

    settings = _settings(tmp_path, request_timeout_seconds=0.2, model_call_timeout_seconds=0.15)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", functools.partial(sleep_past_deadline, 2.0, then=_count_plan("IfcDoor")))
    container, service = _service(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.conversations = {}
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(question=QUESTION)))

    assert response.disposition.value == "timeout"
    assert response.disposition.value != "error"


# --- F8: valid-but-unsupported capability -------------------------------------------

def test_f8_unsupported_capability_is_refused_with_rationale_and_no_tool_call(tmp_path: Path) -> None:
    """SPEC-M1.5 §4A: a genuinely unsupported capability (not a clarification,
    not a transport/parsing error) now surfaces as its own disposition,
    "unsupported", distinct from the generic "refused" category (D-010).
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    reason = "Nearest-room search across the whole project is not supported."
    unsupported_plan = MultiQueryPlan(intent="unsupported", response_language="en", rationale=reason, subplans=[QueryPlan(
        subtask_id="task_1", source="unsupported", intent="unsupported", rationale=reason,
        planning_mode="llm", match_status="complete",
    )])
    fake.script("multi_query_plan", unsupported_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f8", question="Please describe the situation in this project regarding room adjacency.", viewer_context=None)

    assert response.disposition.value == "unsupported"
    assert reason in response.answer_markdown
    assert response.execution_metadata.get("tool_call_count") == 0


# --- F9: cross-source join rejected before tool execution ---------------------------

def test_f9_cross_source_join_rejected_before_any_tool_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()  # scripted with nothing: a tool/model call would raise AssertionError
    container, service = _service(settings, fake)

    response = service.invoke(
        thread_id="f9", viewer_context=None,
        question="Please join the PDF connected load data with the IFC room area.",
    )

    assert response.disposition.value == "unsupported"
    assert response.execution_metadata.get("tool_call_count") == 0
    assert response.execution_metadata.get("model_call_count") == 0
    trace = container.audit_store.by_trace(response.trace_id)
    assert not any(event.event_type == "tool_called" for event in trace)


# --- F10: client cancellation mid-invocation ----------------------------------------

def test_f10_cancellation_produces_cancelled_disposition_without_context_mutation(tmp_path: Path) -> None:
    import app.main as main_module

    settings = _settings(tmp_path, request_timeout_seconds=30, model_call_timeout_seconds=30)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", functools.partial(sleep_past_deadline, 2.0, then=_count_plan("IfcDoor")))
    container, service = _service(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.conversations = {}
    main_module.app.state.requests = {}

    async def run() -> None:
        task = asyncio.create_task(main_module.chat(ChatRequest(request_id="req-f10", thread_id="thread-f10", question=QUESTION)))
        await asyncio.sleep(0.1)
        task.cancel()
        response = await task
        assert response.disposition.value == "cancelled"
        assert "thread-f10" not in main_module.app.state.conversations

    asyncio.run(run())


# --- F11: tool result shape mismatches the plan's expected_result_shape -------------

def test_f11_result_shape_mismatch_is_not_answered(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    mismatched_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcDoor",
        filters={}, group_by="none", expected_result_shape="scalar_measurement",
        rationale="mismatched shape", planning_mode="llm", match_status="complete",
    )])
    fake.script("multi_query_plan", mismatched_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="f11", question=QUESTION, viewer_context=None)

    assert response.disposition.value == "error"
    assert response.disposition.value != "answered"
    assert response.verification.status != "passed"


# --- F12: PDF vision path fails / returns low-confidence evidence -------------------

def test_f12_low_confidence_vision_extraction_clarifies_without_fabricating_a_value(tmp_path: Path) -> None:
    """SPEC-M2P1 scenario rewrite, authorized as a scoped exception to §10.

    The original scenario asked for "the diversity factor situation for
    Panel-A" -- a question that (both before and after M2P1) is heuristically
    fast-pathed to the PDF source without ever reaching the semantic planner,
    so the scripted ``multi_query_plan`` response below was already dead
    scaffolding in the M1 baseline. What actually decided the outcome was
    ``requested_field``: under M2P1's deterministic-first extraction, that
    question names both a real field (Diversity Factor) and a real record
    (Panel-A) unambiguously, so it is now correctly *answered* (0.70,
    matching SPEC-M2P1 §3 B9 ground truth) instead of reaching vision. That
    is the new architecture working as specified, not a regression -- see
    the SPEC-M2P1 continuation report.

    F12's actual intent -- the vision fallback must never fabricate a value
    on a low-confidence/ambiguous result -- is preserved here with a
    scenario deterministic extraction genuinely cannot resolve: an
    "after diversity load" field, which SPEC-M2P1 §3 B9 establishes does not
    exist as a column in the fixture at all. That is an unconditional miss
    (confidence 0.0, no candidate column), which reaches the *generic*
    vision fallback in ``_execute_pdf`` (no board is named for Panel-A
    either, since OD-9 does not widen ``target_board``'s regex, so the
    board-localized vision flow is not the one exercised here).
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("pdf_extract", VisionFieldExtraction(
        value=None, unit=None, page=1, bbox=None, confidence=0.2, rationale="ambiguous",
        ambiguity="Could not visibly confirm an after-diversity-load value for Panel-A on this page.",
    ))
    fake.script("pdf_verify", VisionEvidenceVerification(supported=False, confidence=0.1, rationale="Not visibly supported on this page."))
    container, service = _service(settings, fake)

    response = service.invoke(
        thread_id="f12", viewer_context=None,
        question="What is the after diversity load for Panel-A in this drawing?",
    )

    assert response.disposition.value == "clarification_required"
    assert not any(char.isdigit() for char in response.answer_markdown)
    for citation in response.citations:
        assert not any(char.isdigit() for char in citation.label)
    purposes_called = [call.purpose for call in fake.calls]
    assert purposes_called == ["pdf_extract", "pdf_verify"]
