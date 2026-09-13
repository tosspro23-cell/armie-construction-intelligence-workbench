"""SPEC-M10 answer-polish tests.

Two things are actually load-bearing here, per this project's verification
standard (a reproduction that doesn't rely on the same code path it's
proving correct): the number-preservation guard's full truth table, and
`_finalize`'s disposition-scoped wiring proven via the fake provider's own
call log, not just final-value inspection -- `FakeModelProvider` raises an
``AssertionError`` for any unscripted purpose, so "the polish path was never
invoked" is something a bug *cannot* silently pass here the way a bare
output-equality check could.

No Ollama, no network, no downloaded model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.schemas.models import AnswerSynthesis, MultiQueryPlan, QueryPlan
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _service(settings: Settings, fake: FakeModelProvider) -> tuple[ServiceContainer, AgentService]:
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    return container, AgentService(container)


# --- the number-preservation guard's truth table ---------------------------------------

@pytest.mark.parametrize(
    ("original", "rewritten", "expected"),
    [
        ("There are 12 doors.", "This project contains 12 doors in total.", True),
        ("Connected load is 51.20 kW.", "The connected load comes out to 51.20 kW.", True),
        ("There are 12 doors.", "This project has several doors.", False),  # dropped
        ("There are 12 doors.", "There are 12 doors across 3 storeys.", False),  # added
        ("There are 12 doors.", "There are 12.0 doors.", False),  # reformatted, not equal
        ("There are 12 doors.", "   ", False),  # empty rewrite
    ],
)
def test_polish_preserves_facts_guard_truth_table(original: str, rewritten: str, expected: bool) -> None:
    assert AgentService._polish_preserves_facts(original, rewritten) is expected


# --- _finalize wiring: disabled by default, proven via the call log --------------------

def test_answer_polish_disabled_by_default_never_calls_the_model(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.enable_answer_polish is False
    fake = FakeModelProvider()  # zero scripted responses: any call raises AssertionError
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="polish-off", question="How many doors are there?", viewer_context=None)

    assert response.disposition.value == "answered"
    assert fake.calls == []
    assert response.execution_metadata.get("answer_polished") is False
    assert response.execution_metadata.get("pre_polish_answer") is None


def test_answer_polish_enabled_rewrites_an_answered_response(tmp_path: Path) -> None:
    # A separate, polish-disabled container/provider fetches the
    # undecorated deterministic answer without touching the real `fake`
    # below at all -- keeps this test's own call-log assertion (exactly
    # one "answer_polish" call) meaningful, rather than baking in a
    # baseline call that would itself attempt to polish.
    baseline_settings = _settings(tmp_path, enable_answer_polish=False)
    baseline_fake = FakeModelProvider()
    baseline_container, baseline_service = _service(baseline_settings, baseline_fake)
    baseline = baseline_service.invoke(
        project_resources=_demo(baseline_container), thread_id="polish-baseline",
        question="How many doors are there?", viewer_context=None,
    )
    original_answer = baseline.answer_markdown

    settings = _settings(tmp_path, enable_answer_polish=True)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)
    fake.script("answer_polish", AnswerSynthesis(answer_markdown=f"Sure -- {original_answer}"))

    response = service.invoke(project_resources=_demo(container), thread_id="polish-on", question="How many doors are there?", viewer_context=None)

    assert response.disposition.value == "answered"
    assert [call.purpose for call in fake.calls] == ["answer_polish"]
    assert response.answer_markdown == f"Sure -- {original_answer}"
    assert response.execution_metadata["answer_polished"] is True
    assert response.execution_metadata["pre_polish_answer"] == original_answer
    # The polish call itself counts toward the total, so the figure the
    # Decision Trace's Result step shows stays accurate (D-024).
    assert response.execution_metadata["model_call_count"] == baseline.execution_metadata["model_call_count"] + 1


def test_answer_polish_rejects_a_fact_dropping_rewrite_and_keeps_the_original(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enable_answer_polish=True)
    fake = FakeModelProvider()
    container, service = _service(settings, fake)
    fake.script("answer_polish", AnswerSynthesis(answer_markdown="There are several doors in this project."))

    response = service.invoke(project_resources=_demo(container), thread_id="polish-rejected", question="How many doors are there?", viewer_context=None)

    assert response.disposition.value == "answered"
    assert [call.purpose for call in fake.calls] == ["answer_polish"]  # the call was made...
    assert response.execution_metadata["answer_polished"] is False  # ...but its output was discarded
    assert response.execution_metadata["pre_polish_answer"] is None
    assert any(char.isdigit() for char in response.answer_markdown)  # original, unpolished text kept


def test_answer_polish_is_never_invoked_for_a_clarification_required_disposition(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enable_answer_polish=True)
    fake = FakeModelProvider()
    mixed_plan = MultiQueryPlan(intent="single_query", response_language="en", rationale="r", subplans=[
        QueryPlan(subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcFurniture", filters={}, group_by="none", expected_result_shape="scalar_count", rationale="unsupported entity type", planning_mode="llm", match_status="complete"),
    ])
    fake.script("multi_query_plan", mixed_plan)
    container, service = _service(settings, fake)

    response = service.invoke(project_resources=_demo(container), thread_id="polish-clarify", question="How many pieces of furniture are there in the project overall?", viewer_context=None)

    assert response.disposition.value in {"clarification_required", "unsupported", "error"}
    # If this ever calls the model, FakeModelProvider raises for the
    # unscripted "answer_polish" purpose -- so a passing test here is
    # itself the proof the polish path was not reached for this disposition.
    assert "answer_polish" not in [call.purpose for call in fake.calls]
