"""Disposition-invariant tests (SPEC-M1.5 §4A/§8).

Covers every terminal disposition in the taxonomy this milestone made
explicit -- ``answered``, ``partially_answered``, ``clarification_required``,
``unsupported``, ``error``, ``refused`` -- with the actual code path that
produces it, plus the Q7-shape zero-model-call regression (§4B/OD-14) and a
reachability proof for the cross-source-join guard consolidation (§4C).

No Ollama, no network, no downloaded model.
"""

from __future__ import annotations

from pathlib import Path

from app.agent.graph import AgentService
from app.agent.router import heuristic_multi_plan, heuristic_plan
from app.config import Settings
from app.schemas.models import MultiQueryPlan, QueryPlan, ResponseLanguage
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider

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


def _service(settings: Settings, fake: FakeModelProvider) -> tuple[ServiceContainer, AgentService]:
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    return container, AgentService(container)


# --- answered -------------------------------------------------------------------------

def test_answered_via_the_deterministic_ifc_fast_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()  # zero scripted responses: fast path must not call a model
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="disp-answered", question="How many doors are there?", viewer_context=None)

    assert response.disposition.value == "answered"
    assert fake.calls == []


# --- partially_answered -----------------------------------------------------------------

def test_partially_answered_when_one_of_two_subplans_is_capability_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    mixed_plan = MultiQueryPlan(intent="multi_query", response_language="en", rationale="r", subplans=[
        QueryPlan(subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcDoor", filters={}, group_by="none", expected_result_shape="scalar_count", rationale="valid", planning_mode="llm", match_status="complete"),
        QueryPlan(subtask_id="task_2", source="ifc", intent="count", operation="count", entity_type="IfcFurniture", filters={}, group_by="none", expected_result_shape="scalar_count", rationale="unsupported entity type", planning_mode="llm", match_status="complete"),
    ])
    fake.script("multi_query_plan", mixed_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="disp-partial", question="Please describe the doors and furniture in this project.", viewer_context=None)

    assert response.disposition.value == "partially_answered"


# --- clarification_required (+ §4B/OD-14 Q7 zero-model-call regression) ---------------

def test_clarification_required_for_a_structural_pdf_record_miss_with_zero_model_calls(tmp_path: Path) -> None:
    """The exact Q7 shape from the M2P1 live-model baseline: a real field is
    named, no record is. Regression for SPEC-M1.5 §4B/OD-14 -- this must
    resolve to clarification_required at ZERO model calls (vision cannot
    resolve "which record did you mean" from the same page), versus 2
    calls/~26s before this milestone.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()  # zero scripted responses: a model call would raise
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="disp-clarify-q7", question="What is the total connected load?", viewer_context=None)

    assert response.disposition.value == "clarification_required"
    assert fake.calls == []
    for board in ("DB-L1-A", "DB-L2-B", "Panel-A"):
        assert board in response.answer_markdown


def test_clarification_required_when_semantic_validation_issues_persist(tmp_path: Path) -> None:
    """The semantic-path equivalent (F5's scenario): _unsupported_subresult
    maps plan.intent == "clarification" to clarification_required, not the
    generic "refused" it collapsed into before this milestone.
    """
    settings = _settings(tmp_path, ollama_escalation_model="qwen3:30b-test")
    bad_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="aggregate", operation="count", entity_type="IfcWindow",
        filters={}, group_by="none", postprocess="argmax", expected_result_shape="scalar_count",
        rationale="bad", planning_mode="llm", match_status="complete",
    )])
    fake = FakeModelProvider()
    fake.script("multi_query_plan", bad_plan)
    fake.script("response_language", ResponseLanguage(code="en"))
    fake.script("multi_query_plan_repair", bad_plan.model_copy(update={"rationale": "still bad"}))
    escalation_fake = FakeModelProvider(name="ollama", model="qwen3:30b-test")
    escalation_fake.script("multi_query_plan_escalation", ConnectionError("escalation model unavailable"))
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake, escalation_provider_factory=lambda s: escalation_fake)
    service = AgentService(container)

    response = service.invoke(thread_id="disp-clarify-semantic", question="Please describe the situation with the windows in this project.", viewer_context=None)

    assert response.disposition.value == "clarification_required"


# --- unsupported ------------------------------------------------------------------------

def test_unsupported_for_a_cross_source_join_with_zero_tool_and_model_calls(tmp_path: Path) -> None:
    """Also the §4C reachability proof: zero tool_call_count and zero
    model_call_count together confirm _resolve_context's early return
    (before "route" -> heuristic_plan/heuristic_multi_plan, and before
    "execute_multi" -> _synthesize_multi_response) is what actually decided
    this response -- not any of the other three call sites.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()  # zero scripted responses: any other path would call a model
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="disp-unsupported-join", question="Please join the PDF connected load data with the IFC room area.", viewer_context=None)

    assert response.disposition.value == "unsupported"
    assert response.execution_metadata.get("tool_call_count") == 0
    assert response.execution_metadata.get("model_call_count") == 0


def test_unsupported_for_a_capability_gate_rejection_via_the_semantic_path(tmp_path: Path) -> None:
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

    response = service.invoke(thread_id="disp-unsupported-capability", question="Please describe the situation in this project regarding room adjacency.", viewer_context=None)

    assert response.disposition.value == "unsupported"


# --- error --------------------------------------------------------------------------------

def test_error_when_bounded_repair_itself_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", ConnectionError("connection refused"))
    fake.script("multi_query_plan_repair", ConnectionError("connection refused"))
    container, service = _service(settings, fake)

    response = service.invoke(thread_id="disp-error", question="Please describe the situation with the stairs in this project.", viewer_context=None)

    assert response.disposition.value == "error"
    assert not any(char.isdigit() for char in response.answer_markdown)


# --- refused: confirmed a pure defensive fallback, no live trigger --------------------

def test_refused_is_only_the_defensive_aggregation_fallback_not_a_live_path(tmp_path: Path) -> None:
    """SPEC-M1.5 finding, recorded here rather than only in a report: after
    §4A-§4C, no live code path emits disposition="refused" anymore -- every
    former "refused" emission is now error/clarification_required/
    unsupported, mapped to its actual cause. The Disposition enum keeps
    REFUSED as a genuine defensive fallback for a malformed subresult
    missing its own "disposition" key entirely, which normal execution
    never produces; this is exercised directly since no realistic
    service.invoke() scenario can reach it.
    """
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)
    multi_plan = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcDoor",
        rationale="r", planning_mode="llm", match_status="complete",
    )])
    malformed_subresult = {"subtask_id": "task_1", "plan": multi_plan.subplans[0].model_dump(), "answer": "malformed", "citations": []}  # no "disposition" key

    result = service._synthesize_multi_response({"question": "irrelevant", "trace_id": "t", "thread_id": "th"}, multi_plan, [malformed_subresult], model_calls=0)

    assert result["disposition"] == "refused"


# --- §4C reachability: the three non-authoritative call sites remain correct ----------

def test_heuristic_plan_and_heuristic_multi_plan_still_correctly_refuse_when_called_directly() -> None:
    """These are unreachable from the live graph (proven above by the
    zero-tool/zero-model-call test), but remain directly callable and must
    still behave correctly for their own callers/tests, independent of the
    graph -- this is why they were not deleted during §4C consolidation.
    """
    question = "Please join the PDF connected load data with the IFC room area."

    single_plan = heuristic_plan(question, {}, has_viewer_context=False)
    assert single_plan.source == "unsupported"
    assert single_plan.intent == "unsupported"

    multi_plan = heuristic_multi_plan(question, {}, has_viewer_context=False)
    assert multi_plan is not None
    assert multi_plan.intent == "unsupported"
    assert len(multi_plan.subplans) == 1
    assert multi_plan.subplans[0].source == "unsupported"
