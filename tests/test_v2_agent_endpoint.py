"""SPEC-M16: end-to-end tests for the V2 tool-calling agent's own request
handler (``_chat_v2``/``POST /api/v1/chat`` with ``engine="v2"``).

Drives the real FastAPI endpoint function directly (matching this
project's own established pattern in
``test_chat_persistence_failure_handling.py`` -- calling
``main_module.chat(...)`` and inspecting its return value, no live HTTP
server needed), with a real ``ServiceContainer``/``AgentService``, a real
IFC fixture, and only the model provider faked -- so this proves the same
"real everything except the model call" claim every other test in this
suite already holds V1 to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.agent.graph import AgentService
from app.config import Settings
from app.schemas.models import ChatRequest
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider, ScriptedAnswer, ScriptedToolCalls

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "How many doors are there?"


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _build(settings: Settings, fake: FakeModelProvider) -> tuple[ServiceContainer, AgentService]:
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    return container, AgentService(container)


async def _collect_sse_events(streaming_response) -> list[dict]:
    events = []
    async for chunk in streaming_response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    return events


def test_v2_engine_answers_correctly_via_the_real_endpoint(tmp_path: Path) -> None:
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    # Independent-review finding, 2026-09-17: was "The project has 6
    # doors." -- the real armie_demo.ifc fixture this test actually
    # executes count_elements against has 4, not 6. Fixed to a real,
    # consistent number now that invoke_v2 checks the final narrative
    # against this turn's own tool results.
    fake.script("v2_tool_turn", ScriptedAnswer(["The project has 4 doors."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="v2-req-1", thread_id="v2-thread-1", question=QUESTION, engine="v2")))
    events = asyncio.run(_collect_sse_events(response))

    tool_statuses = [event for event in events if event["type"] == "tool_status"]
    assert {status["status"] for status in tool_statuses} == {"started", "completed"}
    final_events = [event for event in events if event["type"] == "final"]
    assert len(final_events) == 1
    final = final_events[0]["response"]
    assert final["disposition"] == "answered"
    assert "4 doors" in final["answer_markdown"]
    assert final["execution_metadata"]["engine"] == "v2"
    assert final["execution_metadata"]["model_call_count"] == 2  # one to decide the tool call, one to answer
    assert len(final["citations"]) > 0


def test_v2_engine_streams_the_answer_incrementally(tmp_path: Path) -> None:
    """The whole point of streaming: multiple distinct answer_chunk events,
    not the full answer delivered as a single chunk.
    """
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedAnswer(["Hello", ", ", "world."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="v2-req-stream", thread_id="v2-thread-stream", question="hi", engine="v2")))
    events = asyncio.run(_collect_sse_events(response))

    chunks = [event["text"] for event in events if event["type"] == "answer_chunk"]
    assert chunks == ["Hello", ", ", "world."]


def test_v2_engine_recent_turn_memory_persists_and_is_reused(tmp_path: Path) -> None:
    """SPEC-M16 SS D: a second turn on the same thread receives the first
    turn's real question/answer as conversation history -- proven by
    inspecting what the fake provider was actually called with, not
    assumed from the persistence code alone.
    """
    import app.main as main_module

    settings = _settings(tmp_path, conversation_memory_turns=6)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    # Independent-review finding, 2026-09-17: this was scripted as "The
    # project has 6 doors." -- the real armie_demo.ifc fixture this test
    # actually executes count_elements against has 4, not 6. Invisible
    # before invoke_v2's own narrative-vs-tool-result consistency check
    # existed (added the same day the review found this); with that check
    # in place, a scripted answer inconsistent with the real tool result
    # now correctly finalizes as disposition=error instead of quietly
    # passing under an unrelated assertion.
    fake.script("v2_tool_turn", ScriptedAnswer(["The project has 4 doors."]))
    # Owner-reported, 2026-09-16: this second turn used to be scripted as
    # a bare ScriptedAnswer with no tool call at all -- unrealistic (V2's
    # own system prompt requires a fresh tool call for every stated fact;
    # a real model asked "and the windows?" calls count_elements again, it
    # does not recall "4" from memory alone), and it happened to be
    # exactly the scenario a since-fixed dead-code disposition bug
    # (`"answered" if all_citations else "answered"`) always mislabeled
    # "answered" regardless. Scripting a real second tool call keeps this
    # test's actual purpose (memory threading) intact while asserting a
    # disposition the fixed code actually earns, not one the bug used to
    # hand out unconditionally.
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcWindow"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["And 4 windows, following up on the doors."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    r1 = asyncio.run(main_module.chat(ChatRequest(request_id="v2-mem-1", thread_id="v2-mem-thread", question=QUESTION, engine="v2")))
    asyncio.run(_collect_sse_events(r1))

    r2 = asyncio.run(main_module.chat(ChatRequest(request_id="v2-mem-2", thread_id="v2-mem-thread", question="and the windows?", engine="v2")))
    events2 = asyncio.run(_collect_sse_events(r2))

    second_call_messages = fake.calls[-1].prompt  # str(messages) -- stream_turn's own RecordedCall.prompt
    assert QUESTION in second_call_messages
    assert "4 doors" in second_call_messages
    final2 = [event for event in events2 if event["type"] == "final"][0]["response"]
    assert final2["disposition"] == "answered"


def test_v2_clarification_required_turn_still_persists_conversation_memory(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, third pass: `_v2_sse_stream`
    only persisted `v2_recent_turns` when `disposition == "answered"` --
    a turn that honestly asked the user to disambiguate
    (disposition=clarification_required) was never saved, so the *next*
    turn's model had no memory of the question it had just asked or why,
    breaking a clarification round-trip's continuity. V1's own `chat()`
    (main.py:550) already persists for both "answered" and
    "clarification_required"; V2 had quietly diverged from that
    established convention.

    space_distance against armie_demo.ifc (which has no IfcSpace elements
    at all) deterministically triggers disposition=clarification_required,
    same technique test_v2_representative_eval.py's own clarification test
    uses -- no synthetic/mocked tool_result needed.
    """
    import app.main as main_module

    settings = _settings(tmp_path, conversation_memory_turns=6)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("space_distance", {"from_space": "Nonexistent Room A", "to_space": "Nonexistent Room B"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Please specify the exact room identifiers you mean."]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Following up on the room clarification, here is more detail."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    r1 = asyncio.run(main_module.chat(ChatRequest(request_id="v2-clarify-mem-1", thread_id="v2-clarify-mem-thread", question="What is the distance between room A and room B?", engine="v2")))
    events1 = asyncio.run(_collect_sse_events(r1))
    final1 = [event for event in events1 if event["type"] == "final"][0]["response"]
    assert final1["disposition"] == "clarification_required"

    r2 = asyncio.run(main_module.chat(ChatRequest(request_id="v2-clarify-mem-2", thread_id="v2-clarify-mem-thread", question="I meant B204 and B202.", engine="v2")))
    asyncio.run(_collect_sse_events(r2))

    second_call_messages = fake.calls[-1].prompt
    assert "What is the distance between room A and room B?" in second_call_messages
    assert "Please specify the exact room identifiers you mean." in second_call_messages


def test_v2_streams_a_flagged_narrative_live_instead_of_withholding_it(tmp_path: Path) -> None:
    """D-062 (2026-09-17), replacing this test's own previous, opposite
    assertion (`test_v2_never_streams_a_narrative_before_it_passes_
    verification`): `answer_chunk` events used to be buffered until the
    numeric narrative-consistency check passed, specifically so a
    narrative later caught as inconsistent with this turn's own tool
    results could be fully withheld from the client -- SPEC-M16's own
    Invariant, "streaming only ever carries already-verified content."

    That invariant was deliberately amended by D-062: across three rounds
    of independent review, every confirmed catch of this exact check was
    against a deliberately scripted adversarial test double (this test's
    own scenario is one of them), never a real fabrication from the
    actual model in live use, while the check's own false-positive rate
    against real live usage was confirmed twice. A decision-support tool
    where the user shares final responsibility for judgment calls is
    better served by disclosing an unconfirmed claim than by silently
    withdrawing it. `AnswerChunkEvent`s are streamed live again (the
    buffering they were held back for no longer applies), and a caught
    inconsistency now sets verification.status="unverified" while keeping
    the disposition the tool-call outcomes actually earned and the
    model's real text -- it is flagged, not hidden.
    """
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 99999 doors, all fire-certified for 120 minutes."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="v2-flagged-1", thread_id="v2-flagged-thread", question=QUESTION, engine="v2")))
    events = asyncio.run(_collect_sse_events(response))

    answer_chunks = [event for event in events if event["type"] == "answer_chunk"]
    assert "".join(chunk["text"] for chunk in answer_chunks) == "There are 99999 doors, all fire-certified for 120 minutes."
    final = [event for event in events if event["type"] == "final"][0]["response"]
    assert final["disposition"] == "answered"
    assert final["verification"]["status"] == "unverified"
    assert "99999" in final["answer_markdown"]
    assert len(final["citations"]) > 0  # real evidence is kept alongside the disclosed caveat, not dropped


def test_v2_request_record_gets_the_real_trace_id_used_by_the_turn(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, second pass: this request's
    own record in `app.state.requests` was registered with
    `trace_id=request_id` (matching chat()'s own initial registration for
    V1) but, unlike chat() (which updates it to the real
    `response.trace_id` on completion), V2's record here never got the
    same update -- invoke_v2 always mints its own separate trace_id, so
    `GET /api/v1/requests/{request_id}` kept reporting a trace_id that
    `/api/v1/traces/{trace_id}` never actually has any audit events under.
    """
    import app.main as main_module

    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["The project has 4 doors."]))
    container, service = _build(settings, fake)
    main_module.app.state.container = container
    main_module.app.state.agent = service
    main_module.app.state.requests = {}

    response = asyncio.run(main_module.chat(ChatRequest(request_id="v2-trace-sync-1", thread_id="v2-trace-sync-thread", question=QUESTION, engine="v2")))
    events = asyncio.run(_collect_sse_events(response))

    final = [event for event in events if event["type"] == "final"][0]["response"]
    record = main_module.app.state.requests["v2-trace-sync-1"]
    assert record["trace_id"] == final["trace_id"]
    assert record["trace_id"] != "v2-trace-sync-1"  # the real trace_id, not the placeholder it started as
    assert record["status"] == "completed"
