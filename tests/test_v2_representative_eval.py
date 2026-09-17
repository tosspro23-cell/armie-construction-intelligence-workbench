"""SPEC-M16 SS G: the representative eval set, CI-safe (FakeModelProvider,
no live Azure call -- that evidence is
docs/reports/2026-09-16-m16-v1-vs-v2-benchmark.md, matching this project's
own established split between CI-safe unit tests and a live-data baseline
report, e.g. tests/test_azure_ai_search_retrieval.py's own header).

Each case scripts the exact tool call(s) a correctly-reasoning model would
make for its question (proven live, with a real model, in the benchmark
report above) and asserts the V2 loop executes them, returns zero
fabricated facts (every value traceable to the real IFC/PDF fixtures), and
reaches `answered`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.graph import AgentService
from app.config import Settings
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider, ScriptedAnswer, ScriptedToolCalls

ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path, fake: FakeModelProvider) -> tuple[ServiceContainer, AgentService, ProjectResources]:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    resources = asyncio.run(container.get_project("demo"))
    return container, service, resources


async def _run(service: AgentService, resources: ProjectResources, question: str, thread_id: str):
    final = None
    async for event in service.invoke_v2(project_resources=resources, thread_id=thread_id, question=question, viewer_context=None):
        if event["type"] == "final":
            final = event["response"]
    return final


@pytest.mark.parametrize("question,tool_call,expected_fragment", [
    ("How many doors are there?", ("count_elements", {"entity_type": "IfcDoor"}), "4"),
    ("What is the maximum door height?", ("aggregate_quantity", {"entity_type": "IfcDoor", "measure": "height", "aggregation": "max"}), "2.1"),
    # Independent-review finding, 2026-09-17: was just "Level" -- the real
    # armie_demo.ifc fixture ties 2 windows on Level 01 against 2 on Level
    # 02, and invoke_v2's own narrative-vs-tool-result consistency check
    # (added the same day) requires a real number from this turn's own
    # group_elements_by_storey result to appear somewhere in the answer,
    # not just a plausible-looking word.
    ("Which storey has the most windows?", ("group_elements_by_storey", {"entity_type": "IfcWindow", "postprocess": "argmax"}), "Level 01 and Level 02 (tied, 2 each)"),
    ("What properties does the door have?", ("get_element_properties", {"entity_type": "IfcDoor"}), "Level"),
])
def test_v2_representative_eval_single_tool_operations(question: str, tool_call: tuple, expected_fragment: str, tmp_path: Path) -> None:
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([tool_call]))
    fake.script("v2_tool_turn", ScriptedAnswer([f"Answering based on real tool data mentioning {expected_fragment}."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, question, f"eval-{question}"))

    assert response.disposition.value == "answered"
    assert response.execution_metadata["engine"] == "v2"
    assert len(response.citations) > 0, "every answered V2 turn in this eval set must cite real tool-derived evidence"


def test_v2_representative_eval_parallel_multi_tool(tmp_path: Path) -> None:
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    fake.script("v2_tool_turn", ScriptedAnswer(["4 doors and 4 windows."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many windows and doors are there?", "eval-parallel"))

    assert response.disposition.value == "answered"
    assert len(response.citations) == 8  # 4 real doors + 4 real windows, both cited
    # Independent-review finding, 2026-09-17: was
    # `state["tool_call_count"] = result.get("tool_call_count", ...)`, an
    # *assignment* inside the loop over this turn's parallel dispatch
    # results -- both calls compute their own "+1" off the same pre-
    # dispatch snapshot (asyncio.gather runs them concurrently against
    # the same `state`), so whichever the loop processed last silently
    # overwrote the other's contribution: two real successful tool calls
    # previously reported tool_call_count=1, not 2.
    assert response.execution_metadata["tool_call_count"] == 2


def test_v2_partial_tool_failure_marks_partially_answered(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17: one real, successful tool
    call alongside one capability-gate-rejected call (an unsupported
    entity type) previously still finalized as disposition="answered" --
    the overall disposition was decided purely from "did *any* tool call
    this turn leave a citation," so a failed/unsupported subtask
    disappeared behind a successful one instead of being reflected in the
    final disposition, unlike V1's own answered/partially_answered/
    unsupported/error vocabulary for the identical situation.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcElevator"}),  # not in SUPPORTED_ENTITY_TYPES -- capability_gate rejects it
    ]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 4 doors; I could not check elevators."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors and elevators are there?", "eval-partial-failure"))

    assert response.disposition.value == "partially_answered"
    assert len(response.citations) == 4  # only the real, successful IfcDoor call contributes citations


def test_v2_summarizes_a_clarification_required_subtask_as_clarification_required(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, second pass: a turn whose
    only dispatched tool call came back disposition="clarification_required"
    (a real, honest "please disambiguate," not a failure) fell through
    every named branch in the disposition-summary logic straight to the
    generic "error" catch-all -- a normal conversational clarification was
    misreported as a system failure.

    space_distance against armie_demo.ifc (which has no IfcSpace elements
    at all) deterministically triggers this exact tool-level disposition
    (`_execute_ifc`'s own except-clause for an unresolvable space name),
    without needing a synthetic/mocked tool_result.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("space_distance", {"from_space": "Nonexistent Room A", "to_space": "Nonexistent Room B"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Please specify the exact room identifiers you mean."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "What is the distance between room A and room B?", "eval-clarification-subtask"))

    assert response.disposition.value == "clarification_required"


def test_v2_representative_eval_reconciliation(tmp_path: Path) -> None:
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("reconcile_doors_windows", {})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Reconciliation found 6 matched, 1 mismatch, 1 missing from each side."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "Compare the doors and windows against the PDF schedule.", "eval-reconcile"))

    assert response.disposition.value == "answered"
    assert len(response.citations) > 0


def test_v2_representative_eval_zero_tool_calls_marks_verification_not_applicable(tmp_path: Path) -> None:
    """SPEC-M16 Invariants: a turn with no tool calls at all -- here, one
    that is really asking the user for more information -- is honestly
    marked: `clarification_required`, matching V1's own disposition for
    the identical situation, and verification `not_applicable` rather than
    a false claim of having checked something.

    Owner-reported, 2026-09-17 (a 24-question EN/ZH live domain sweep):
    this scenario is exactly SPEC-M16's own live benchmark report's
    "known gap, found live, not yet fixed" (question 10, docs/reports/
    2026-09-16-m16-v1-vs-v2-benchmark.md) -- `invoke_v2`'s disposition line
    was `"answered" if all_citations else "answered"`, both branches
    identical, so a zero-tool-call turn was always mislabeled "answered"
    regardless of citations. The sweep found this is a broader pattern (5
    of 24 real turns), never a case where V2 legitimately answered without
    needing data -- always a genuine clarification or capability-limit
    explanation. Fixed at the source; this test now asserts the corrected
    disposition instead of the bug it used to encode.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedAnswer(["Please select an element or capture the current view first."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "What can you see in the current view?", "eval-viewer"))

    assert response.disposition.value == "clarification_required"
    assert response.verification.status == "not_applicable"
    assert response.citations == []


def test_v2_rejects_narrative_that_contradicts_its_own_tool_result(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17: a scripted model that calls
    a real tool (count_elements -> the real armie_demo.ifc fixture's real
    4 doors), then answers with a wildly different, unrelated number
    ("99999 doors, all fire-certified for 120 minutes") previously still
    finalized as disposition=answered, verification.status="verified",
    with 4 real citations attached -- real evidence lending false
    credibility to a fabricated conclusion, since the only check ever
    performed was "did a tool call leave a citation this turn," never
    whether the model's own final prose was consistent with it.

    This locks in the fix (`_narrative_consistent_with_tool_facts`): the
    turn must now finalize as disposition=error, verification.status
    ="failed", and the fabricated text must never reach `answer_markdown`.

    Scope, stated plainly (see `_numeric_tokens_from_result_value`'s own
    docstring): this is a numeric-only cross-check. It reliably catches a
    wrong *number* (this test's own scenario); it cannot and does not
    catch a fabricated *non-numeric* claim riding along with a correct
    number (e.g. an invented fire-rating attached to a correct door
    count) -- that gap is real and not closed by this test or the fix it
    verifies.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 99999 doors, all fire-certified for 120 minutes."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-fabrication"))

    assert response.disposition.value == "error"
    assert response.verification.status == "failed"
    assert "99999" not in response.answer_markdown
    assert response.citations == []  # real evidence must not sit alongside a withdrawn, inconsistent claim


def test_v2_rejects_a_fabricated_number_hidden_behind_a_correct_decoy(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, third pass: the first
    version of the consistency check (fixed above) only required "some
    real number from this turn appears *somewhere* in the answer" --
    bypassable by mentioning the real number as an unrelated aside while
    stating a fabricated one as the actual claim. Reproduced live: a real
    count_elements call returns 4; the model answers "4 records were
    checked. There are 99999 doors, all certified for 120 minutes." -- a
    genuine "4" is present, so the first check alone passed clean.

    Fixed by additionally requiring that wherever the answer names this
    tool call's own entity noun ("door"/"doors") next to a number, that
    number is a real one -- "99999 doors" fails this even though "4"
    exists elsewhere in the same text.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["4 records were checked. There are 99999 doors, all certified for 120 minutes."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-fabrication-decoy"))

    assert response.disposition.value == "error"
    assert response.verification.status == "failed"
    assert "99999" not in response.answer_markdown


def test_narrative_consistency_check_tolerates_natural_rounding_of_a_measurement() -> None:
    """Owner-reported, 2026-09-17 (found live): a real space_distance call
    returning {"value_m": 5.234, ...} (the same shape aggregate_quantity
    uses) was phrased by the model as "5.23 m" -- a natural, honest
    rounding choice, not fabrication -- and the check's first version
    (exact string match against 3 pre-formatted decimal-place guesses)
    rejected it as inconsistent, turning a correct answer into a false
    disposition=error. A count (whole number) still requires an exact
    match: "4 doors" vs "5 doors" is a real discrepancy, not rounding.
    """
    no_terms: frozenset[str] = frozenset()
    facts = AgentService._numeric_tokens_from_result_value({"value_m": 5.234, "from_space": "B204", "to_space": "B202"})
    assert AgentService._narrative_consistent_with_tool_facts("B204 到 B202 的距离约为 5.23 米。", [(no_terms, facts)])
    assert AgentService._narrative_consistent_with_tool_facts("The distance is 5.2 m.", [(no_terms, facts)])
    assert AgentService._narrative_consistent_with_tool_facts("The distance is 5.234 m.", [(no_terms, facts)])
    assert not AgentService._narrative_consistent_with_tool_facts("The distance is 12 m.", [(no_terms, facts)])

    door_terms = AgentService._entity_terms_for("IfcDoor")
    door_count_facts = AgentService._numeric_tokens_from_result_value(4)
    assert AgentService._narrative_consistent_with_tool_facts("There are 4 doors.", [(door_terms, door_count_facts)])
    assert not AgentService._narrative_consistent_with_tool_facts("There are 5 doors.", [(door_terms, door_count_facts)])


def test_v2_respects_an_expired_deadline(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17: V2 was bounded only by
    `tool_calling_max_iterations` (iteration *count*), never wall-clock
    time -- confirmed live: a 30ms deadline with a 150ms-delayed fake
    model still produced a normal `final` ~282ms later, no timeout.
    `invoke_v2`'s own `deadline` param (an absolute `time.perf_counter()`
    timestamp, checked once per iteration) closes this; a deadline already
    in the past when the turn starts must produce `disposition=timeout`
    before any model call is even attempted.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedAnswer(["This should never be reached."]))
    _, service, resources = _service(tmp_path, fake)

    import time as _time

    final = None
    async def _run_with_deadline():
        nonlocal final
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-timeout", question="How many doors are there?",
            viewer_context=None, deadline=_time.perf_counter() - 1.0,
        ):
            if event["type"] == "final":
                final = event["response"]
    asyncio.run(_run_with_deadline())

    assert final.disposition.value == "timeout"
    assert not fake.calls  # the deadline check runs before the model is ever called


def test_v2_respects_a_deadline_that_expires_mid_model_call(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, second pass: the first
    version of the deadline check above only ran *between* iterations --
    a single iteration's own model call sleeping/hanging past `deadline`
    was never interrupted. Reproduced live with the project's IFC cache
    pre-warmed (matching the reviewer's own repro note): a still-valid
    50ms deadline at iteration start, a 200ms-delayed fake model, and the
    turn still finalized normally (disposition=clarification_required)
    ~200ms later -- no timeout at all.

    Fixed by wrapping each received stream event in `asyncio.wait_for`
    against the remaining budget, not just checking at the loop's own
    iteration boundary. `resources.ifc_repository.metadata()` is called
    once before timing starts, deliberately, to warm the same
    IfcOpenShell-parsing cache invoke_v2's own storey-name lookup reads --
    otherwise that parse's own real latency (order 100ms for even this
    small fixture) can by itself exceed a 50ms deadline before the loop
    is ever reached, masking whether *this* fix (mid-call, not pre-call)
    is the one actually doing the catching.
    """
    import functools
    import time as _time

    from fakes.fake_provider import sleep_past_deadline

    fake = FakeModelProvider()
    fake.script("v2_tool_turn", functools.partial(sleep_past_deadline, 0.2, then=ScriptedAnswer(["This should never be reached in time."])))
    _, service, resources = _service(tmp_path, fake)
    resources.ifc_repository.metadata()  # warm the cache -- see docstring above

    final = None
    async def _run_with_deadline():
        nonlocal final
        deadline = _time.perf_counter() + 0.05
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-mid-call-timeout", question="How many doors are there?",
            viewer_context=None, deadline=deadline,
        ):
            if event["type"] == "final":
                final = event["response"]
    t0 = _time.perf_counter()
    asyncio.run(_run_with_deadline())
    elapsed = _time.perf_counter() - t0

    assert final.disposition.value == "timeout"
    assert elapsed < 0.15  # nowhere near the fake model's own full 200ms delay
    assert "This should never be reached in time." not in final.answer_markdown


def test_v2_respects_a_cancellation_request(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17: `/api/v1/requests/{id}/cancel`
    404ed for any V2 request_id (V2 was never registered in
    `app.state.requests` at all). `invoke_v2`'s own `cancel_check` param
    (a callable the caller wires to that same request record) closes this
    at the `AgentService` level; main.py's own wiring is covered by the
    full endpoint tests in test_v2_agent_endpoint.py.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedAnswer(["This should never be reached."]))
    _, service, resources = _service(tmp_path, fake)

    final = None
    async def _run_with_cancel():
        nonlocal final
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-cancel", question="How many doors are there?",
            viewer_context=None, cancel_check=lambda: True,
        ):
            if event["type"] == "final":
                final = event["response"]
    asyncio.run(_run_with_cancel())

    assert final.disposition.value == "cancelled"
    assert not fake.calls
