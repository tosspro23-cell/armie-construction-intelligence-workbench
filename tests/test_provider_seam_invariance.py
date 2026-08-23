"""Seam-invariance tests (SPEC-M1 §4.7).

These are the testable replacement for any unfalsifiable "live-provider
behaviour unchanged" claim: they assert the injected-factory seam resolves
providers identically to the pre-refactor direct factory calls, and that
the audit-field set/values for one scripted end-to-end run match a
committed snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.providers.factory import get_text_provider, get_vision_provider
from app.providers.ollama_provider import StructuredOutputError
from app.schemas.models import MultiQueryPlan, QueryPlan
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, llm_provider: str) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf",
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
        llm_provider=llm_provider, openai_api_key="sk-test-not-a-real-key",
    )
    settings.ensure_runtime_directories()
    return settings


@pytest.mark.parametrize("llm_provider", ["ollama", "hybrid", "openai"])
def test_seam_resolves_text_provider_identically_to_the_pre_refactor_factory(llm_provider: str, tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_provider)
    container = ServiceContainer(settings)  # default factories: the seam under test

    seam_provider = container.text_provider_factory(settings)
    direct_provider = get_text_provider(settings)

    assert seam_provider.name == direct_provider.name
    assert seam_provider.model == direct_provider.model
    assert getattr(seam_provider, "timeout_seconds", None) == getattr(direct_provider, "timeout_seconds", None)


@pytest.mark.parametrize("llm_provider", ["ollama", "hybrid", "openai"])
def test_seam_resolves_vision_provider_identically_to_the_pre_refactor_factory(llm_provider: str, tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_provider)
    container = ServiceContainer(settings)

    seam_provider = container.vision_provider_factory(settings)
    direct_provider = get_vision_provider(settings)

    assert seam_provider.name == direct_provider.name
    assert seam_provider.model == direct_provider.model
    assert getattr(seam_provider, "timeout_seconds", None) == getattr(direct_provider, "timeout_seconds", None)


def test_seam_container_default_factories_are_the_production_factory_functions(tmp_path: Path) -> None:
    """The seam changes *who calls* the factory, never *how it chooses*
    (SPEC-M1 §4.2/8): ServiceContainer's defaults must be the same function
    objects as providers/factory.py's production factories, not a
    reimplementation that could drift from them.
    """
    settings = _settings(tmp_path, "ollama")
    container = ServiceContainer(settings)
    assert container.text_provider_factory is get_text_provider
    assert container.vision_provider_factory is get_vision_provider


# --- Audit-field snapshot for one scripted end-to-end run ---------------------------

def test_audit_field_snapshot_for_one_scripted_end_to_end_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "ollama")
    fake = FakeModelProvider(name="ollama", model="qwen3:8b-test")
    fake.script("multi_query_plan", StructuredOutputError("bad json"))
    repaired = MultiQueryPlan(response_language="en", rationale="r", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcDoor",
        filters={}, group_by="none", expected_result_shape="scalar_count", rationale="r",
        planning_mode="llm", match_status="complete",
    )])
    fake.script("multi_query_plan_repair", repaired)
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(thread_id="snapshot", question="Please describe the situation with the doors in this project.", viewer_context=None)
    assert response.disposition.value == "answered"

    trace = container.audit_store.by_trace(response.trace_id)
    assert trace, "expected at least one audit event for this trace"

    # Keys: every emitted AuditEvent carries exactly this field set.
    expected_keys = {
        "id", "trace_id", "thread_id", "timestamp", "step", "event_type", "summary",
        "payload", "duration_ms", "configured_provider", "actual_provider", "actual_model",
        "planning_mode", "model_call_count", "tool_call_count", "retry_count",
        "provider_fallback_reason",
    }
    for event in trace:
        assert set(event.model_dump().keys()) == expected_keys

    # Provider/model/retry values for the model-touching steps of this
    # specific scripted run (free-text summaries are deliberately excluded).
    model_touching = [
        (event.step, event.event_type, event.configured_provider, event.actual_provider, event.actual_model, event.retry_count)
        for event in trace if event.actual_provider is not None
    ]
    expected_snapshot = [
        ("semantic_plan", "model_called", "ollama", "ollama", "qwen3:8b-test", 0),
        ("semantic_plan", "model_failed", "ollama", "ollama", "qwen3:8b-test", 1),
        ("semantic_repair", "semantic_repair", "ollama", "ollama", "qwen3:8b-test", 1),
    ]
    assert model_touching == expected_snapshot
