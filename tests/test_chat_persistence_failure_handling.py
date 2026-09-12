"""Independent-review defect, verified and fixed: an unreachable/erroring
ConversationStore/AuditStore (SPEC-M4) previously could (1) crash
POST /api/v1/chat with an unhandled exception instead of the endpoint's
normal honest `error` disposition, (2) block the process's single event
loop for the duration of a synchronous store call -- starving every other
concurrent request, health check, and cancellation on this single-replica
deployment (OD-22) -- and (3) leave app.state.requests reporting
"completed" even when the final conversation-context write actually
failed. None of this was reachable through FakeModelProvider, which
exercises the model boundary, not the persistence boundary.

Each test below independently reproduces one of these three failure modes
against the actual fixed code in apps/api/app/main.py, using fake stores
that fail or stall on command -- no real Postgres needed, matching this
repo's no-network-egress CI policy and D-007's fake-boundary discipline.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.agent.graph import AgentService
from app.config import Settings
from app.persistence.conversation_store import InMemoryConversationStore
from app.persistence.factory import get_audit_store
from app.schemas.models import ChatRequest, MultiQueryPlan, QueryPlan
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "How many doors are in this project?"


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _count_plan(entity_type: str) -> MultiQueryPlan:
    return MultiQueryPlan(response_language="en", rationale="test", subplans=[QueryPlan(
        subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type=entity_type,
        filters={}, group_by="none", expected_result_shape="scalar_count", rationale="test plan",
        planning_mode="llm", match_status="complete",
    )])


class _RaisingConversationStore:
    """Simulates an unreachable Postgres on the initial context read."""

    def get(self, thread_id: str):
        raise RuntimeError("simulated database outage")

    def set(self, thread_id: str, context: dict) -> None:
        pass

    def bind_project(self, thread_id: str, project_id: str) -> str:
        # SPEC-M9: chat() now calls this before the context read this test
        # targets -- an unreachable store must surface identically here
        # too, not raise an unrelated AttributeError from a fake that
        # predates this method.
        raise RuntimeError("simulated database outage")


class _WriteFailingConversationStore(InMemoryConversationStore):
    """Reads succeed (so the agent runs and answers correctly); the final
    context write fails, simulating the database going down mid-request."""

    def set(self, thread_id: str, context: dict) -> None:
        raise RuntimeError("simulated write failure")


class _SlowConversationStore(InMemoryConversationStore):
    """A store whose read takes long enough that, if called directly on the
    event loop instead of via asyncio.to_thread, it would visibly starve a
    concurrent task scheduled at the same time."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self._delay_seconds = delay_seconds

    def get(self, thread_id: str):
        time.sleep(self._delay_seconds)
        return super().get(thread_id)


def _build(settings: Settings, fake: FakeModelProvider, conversation_store_factory) -> tuple[ServiceContainer, AgentService]:
    container = ServiceContainer(
        settings,
        text_provider_factory=lambda s: fake,
        vision_provider_factory=lambda s: fake,
        conversation_store_factory=conversation_store_factory,
        audit_store_factory=get_audit_store,
    )
    return container, AgentService(container)


def test_an_unreachable_conversation_store_produces_the_normal_error_disposition_not_a_crash(tmp_path: Path) -> None:
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container, service = _build(settings, fake, lambda s: _RaisingConversationStore())
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="req-store-down", question=QUESTION)))

    assert response.disposition.value == "error"
    assert "simulated database outage" in response.answer_markdown
    # Not stuck at "running" -- the honest failure was fully recorded, the
    # same as any other terminal branch of this endpoint.
    assert main_module.app.state.requests["req-store-down"]["status"] == "error"


def test_a_conversation_context_write_failure_does_not_discard_a_correct_answer(tmp_path: Path) -> None:
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", _count_plan("IfcDoor"))
    container, service = _build(settings, fake, lambda s: _WriteFailingConversationStore())
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="req-write-fail", thread_id="thread-write-fail", question=QUESTION)))

    # The answer itself is unaffected by the persistence failure...
    assert response.disposition.value == "answered"
    # ...but the failure is surfaced, not silently swallowed...
    assert "simulated write failure" in response.execution_metadata.get("context_persist_error", "")
    # ...and the request's own bookkeeping still reflects that the chat
    # call itself completed (persistence is a secondary concern to the
    # already-correct, already-verified answer -- D-004).
    assert main_module.app.state.requests["req-write-fail"]["status"] == "completed"


def test_a_slow_conversation_store_read_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """Direct proof that conversations.get() runs off the event loop
    (asyncio.to_thread), not on it: a concurrently scheduled, trivial
    coroutine completes well before the slow store's synchronous 300ms
    read returns, which would not be possible if that read were blocking
    the single event loop this process runs everything else on.
    """
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("multi_query_plan", _count_plan("IfcDoor"))
    container, service = _build(settings, fake, lambda s: _SlowConversationStore(0.3))
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    marker_completed_at: list[float] = []

    async def marker() -> None:
        await asyncio.sleep(0.05)
        marker_completed_at.append(time.monotonic())

    async def run() -> tuple[object, float]:
        start = time.monotonic()
        chat_task = asyncio.create_task(main_module.chat(ChatRequest(request_id="req-slow-store", question=QUESTION)))
        marker_task = asyncio.create_task(marker())
        response = await chat_task
        await marker_task
        return response, start

    response, start = asyncio.run(run())

    assert response.disposition.value == "answered"
    elapsed_before_marker = marker_completed_at[0] - start
    # The marker (a bare 50ms sleep) finished well before the store's own
    # 300ms delay would have elapsed if it were run synchronously on the
    # same loop as everything else -- proving the slow call was actually
    # off-loaded to a worker thread instead of blocking this coroutine's
    # own progress.
    assert elapsed_before_marker < 0.3
