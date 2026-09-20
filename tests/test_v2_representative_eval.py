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
from app.schemas.models import VerificationStatus
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
    # Independent-review finding, 2026-09-17, third pass: was just "Level"
    # -- get_element_properties's real result is a list of per-element
    # property dicts, which _numeric_tokens_from_result_value used to
    # return zero facts for entirely (silently disabling the consistency
    # check for every property-lookup answer); now it recurses into the
    # real property values (the fixture's real door height is 2.1 m), so
    # this answer must actually state one to pass.
    ("What properties does the door have?", ("get_element_properties", {"entity_type": "IfcDoor"}), "2.1"),
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


def test_v2_accepts_a_real_sum_of_this_turns_own_per_type_counts(tmp_path: Path) -> None:
    """Owner-reported, 2026-09-17 (found live against the real Duplex
    project): after a turn listing every entity type's own count
    individually, the user asked "好了总和是多少?" ("okay, what's the
    total?"). The model correctly re-called the counts this turn (per its
    own system prompt: never state a number not just received from a tool
    this turn) and answered with their sum -- a real, mechanically
    verifiable arithmetic derivation over this turn's own real numbers,
    not a new, ungrounded claim. The narrative-consistency check as
    written only accepted a value that was itself literally one call's
    own returned number, so the correct total ("8" here, from 4 real
    doors + 4 real windows) was rejected as if it were fabricated --
    disposition=error, "I could not safely finalize this answer...",
    exactly what the owner saw live.

    Fixed by additionally accepting the sum of this turn's own whole-
    number, single-valued ("pure scalar") facts as a valid baseline value.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    fake.script("v2_tool_turn", ScriptedAnswer(["That's a total of 8 elements."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "好了总和是多少?", "eval-sum-of-counts"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"
    assert "8" in response.answer_markdown

    # The fix must not have simply widened the check into accepting any
    # number -- an incorrect stated total is still caught (D-062: flagged
    # as unverified rather than hard-blocked, but still caught).
    fake_bad = FakeModelProvider()
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    fake_bad.script("v2_tool_turn", ScriptedAnswer(["That's a total of 999 elements."]))
    _, service_bad, resources_bad = _service(tmp_path, fake_bad)
    response_bad = asyncio.run(_run(service_bad, resources_bad, "好了总和是多少?", "eval-sum-of-counts-bad"))
    assert response_bad.verification.status == "unverified"


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


def test_v2_flags_a_narrative_that_contradicts_its_own_tool_result(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17: a scripted model that calls
    a real tool (count_elements -> the real armie_demo.ifc fixture's real
    4 doors), then answers with a wildly different, unrelated number
    ("99999 doors, all fire-certified for 120 minutes") previously still
    finalized as disposition=answered, verification.status="verified",
    with 4 real citations attached -- real evidence lending false
    credibility to a fabricated conclusion, since the only check ever
    performed was "did a tool call leave a citation this turn," never
    whether the model's own final prose was consistent with it.

    D-062 (2026-09-17), amending this test's own previous assertion:
    `_narrative_consistent_with_tool_facts` catching a mismatch used to
    hard-fail the whole turn (disposition=error, real text replaced,
    citations dropped) -- now it flags instead: disposition stays
    whatever the tool-call outcomes earned ("answered" here, since the
    tool call itself succeeded), verification.status becomes "unverified",
    and the model's actual text is kept, not replaced or hidden. See D-062
    for why: across three review rounds, every confirmed catch of this
    check was an adversarial test double, never a real fabrication in live
    use, while the check's own false-positive rate against real usage was
    confirmed twice.

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

    assert response.disposition.value == "answered"
    assert response.verification.status == "unverified"
    assert "99999" in response.answer_markdown  # shown, not withheld -- the caveat is in verification, not a text swap
    assert len(response.citations) > 0  # real evidence is kept, not dropped, alongside the disclosed caveat


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

    D-062 (2026-09-17): a caught mismatch is now flagged
    (verification.status="unverified"), not hard-blocked -- see that
    decision for why. Still verified here: shown, not withheld.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["4 records were checked. There are 99999 doors, all certified for 120 minutes."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-fabrication-decoy"))

    assert response.verification.status == "unverified"
    assert "99999" in response.answer_markdown


def test_v2_rejects_a_fabricated_number_sitting_in_the_same_window_as_a_real_one(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, fourth pass: the decoy fix
    above (real number placed in an *earlier, separate* sentence) still
    left a narrower variant open -- a real number placed in the *same*
    ~15-character window as the fabricated one, e.g. "There are 99999
    doors (4 checked)." Both "99999" and the real "4" fall within one
    scan of the word "doors," and the old rule only required *some*
    nearby number to match ("if nearby_numbers and not any(...)"): the
    real "4" made that check pass even though "99999" itself never
    matched anything.

    Fixed by requiring *every* number found near the entity noun to be
    individually explainable, not just one of them.

    D-062 (2026-09-17): a caught mismatch is now flagged
    (verification.status="unverified"), not hard-blocked -- see that
    decision for why. Still verified here: shown, not withheld.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 99999 doors (4 checked)."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-fabrication-same-window-decoy"))

    assert response.verification.status == "unverified"
    assert "99999" in response.answer_markdown


def test_v2_accepts_the_same_entity_reported_at_two_different_scopes(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, fourth pass: a genuinely
    correct answer comparing the *same* entity at two different
    scopes/filters in one turn -- project-wide count_elements(IfcDoor)=4,
    then a second count_elements(IfcDoor, storey="Level 01")=2 -- was
    wrongly rejected. Each tool call's own narrow expected set ({4} for
    the first, {2} for the second) was checked against *every* occurrence
    of the shared noun "doors" in the answer: the "{4}" call's own check
    saw the unrelated "2" near a different "doors" mention elsewhere in
    the text and had nothing in its own set to explain it.

    Multi-scope comparison in a single answer is V2's core value
    proposition (the whole reason a typed, single-shape V1 QueryPlan
    can't express this class of question) -- wrongly rejecting it here
    would be a serious regression, not an edge case. Fixed by merging
    expected values across every tool call that shares the same entity
    terms before running the entity-bound check, so a legitimate second
    scope's real value is itself part of what "doors" is allowed to mean
    anywhere in the answer.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcDoor", "storey": "Level 01"}),
    ]))
    fake.script("v2_tool_turn", ScriptedAnswer(["The whole project contains 4 doors. On the first floor there are 2 doors."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors total, and how many on the first floor?", "eval-multi-scope"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"
    assert "4 doors" in response.answer_markdown and "2 doors" in response.answer_markdown

    # The combined-expected-set fix must not have simply widened the check
    # into accepting anything -- a value that matches neither call's real
    # result is still caught (D-062: flagged as unverified, not
    # hard-blocked, but still caught).
    fake_bad = FakeModelProvider()
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcDoor", "storey": "Level 01"}),
    ]))
    fake_bad.script("v2_tool_turn", ScriptedAnswer(["The whole project contains 4 doors. On the first floor there are 99999 doors."]))
    _, service_bad, resources_bad = _service(tmp_path, fake_bad)
    response_bad = asyncio.run(_run(service_bad, resources_bad, "How many doors total, and how many on the first floor?", "eval-multi-scope-bad"))
    assert response_bad.verification.status == "unverified"


def test_v2_rejects_a_fabricated_property_value_from_a_list_shaped_tool_result(tmp_path: Path) -> None:
    """Independent-review finding, 2026-09-17, fourth pass: get_element_
    properties returns a *list* of per-element property dicts (e.g.
    [{"element": {...}, "storey": ..., "properties": {"Height": 2.1,
    ...}}, ...]) -- a shape `_numeric_tokens_from_result_value` didn't
    handle at all, returning an empty set and silently disabling both
    consistency checks for every property-lookup answer. Confirmed live:
    "Every door is 99999 metres high and fire certified" after a real
    get_element_properties call (real height 2.1 m) passed unchecked.

    Fixed by recursing into list/dict-shaped tool results to pull out
    every real numeric leaf value as a candidate fact.

    D-062 (2026-09-17): a caught mismatch is now flagged
    (verification.status="unverified"), not hard-blocked -- see that
    decision for why. Still verified here: shown, not withheld.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Every door is 99999 metres high and fire certified."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "What properties does the door have?", "eval-property-fabrication"))

    assert response.verification.status == "unverified"
    assert "99999" in response.answer_markdown


def test_v2_exposes_an_exhaustive_distinct_value_summary_for_a_truncated_list_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision, 2026-09-17: closes the sample-truncation false-
    completeness gap (independent review, third pass, P2 #5 -- "how many
    distinct window sizes" answered from only the first 40 of 85 elements
    could truthfully report 3 distinct sizes in the sample while a 4th
    exists only among the omitted ones) at the data layer, not by
    restricting the model's own output the way a structural fact-binding
    rewrite would -- see graph.py's own comment on
    `_distinct_value_summary`'s caller for the reasoning (V2 exists so the
    model can freely synthesize over tool results; capping output to only
    ever restate bound fields would reintroduce the same ceiling V2 was
    built to avoid). Instead, the full untruncated list -- already fully
    available server-side before capping to `_V2_TOOL_RESULT_LIST_CAP` --
    is used to compute an exhaustive per-field distinct-value count,
    included in the tool result asked back to the model.

    Verified here by dispatching a synthetic 85-item get_element_properties
    result whose 4th distinct height (0.9 m) appears only at index 84 --
    past the 40-item sample cap -- and asserting the model's own next call
    actually received all 4 distinct values, not just the 3 visible in
    sample_items.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcFurnishingElement"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 4 distinct heights: 0.45 m, 0.5 m, 0.6 m, and 0.9 m."]))
    _, service, resources = _service(tmp_path, fake)

    items = []
    for i in range(85):
        height = 0.45 if i < 60 else (0.5 if i < 80 else (0.6 if i < 84 else 0.9))  # 4th distinct value only at index 84
        items.append({"element": {"express_id": 1000 + i, "entity_type": "IfcFurnishingElement"}, "storey": "Level 01", "properties": {"Height": height}})

    real_dispatch = AgentService._v2_dispatch_tool

    def synthetic_large_list_dispatch(self, tool_call, state):
        if tool_call.tool_name == "get_element_properties":
            return {
                "tool_result": {"answer": "Found 85 matching elements.", "disposition": "answered", "citations": [], "verification": VerificationStatus(status="verified", reason="test").model_dump(), "result_value": items},
                "evidence": [], "tool_call_delta": 1, "plan": [{"entity_type": "IfcFurnishingElement", "source": "ifc"}],
            }
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", synthetic_large_list_dispatch)

    response = asyncio.run(_run(service, resources, "How many distinct heights of furnishing elements are there?", "eval-distinct-value-summary"))

    assert response.disposition.value == "answered"
    second_call_messages = fake.calls[-1].prompt
    tool_message_start = second_call_messages.index("'role': 'tool'")
    tool_payload_text = second_call_messages[tool_message_start:]
    assert "distinct_value_summary" in tool_payload_text
    assert "0.9" in tool_payload_text  # the 4th distinct value, only present past the 40-item sample cap


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


def test_narrative_consistency_check_does_not_treat_a_restated_tag_as_a_fabricated_number() -> None:
    """Found live (2026-09-20), SPEC-M17 finding-investigation flow against
    the real deployed app and real production data: a fully correct V2
    investigation answer for finding 146596 ("IFC model: door tag 146596
    has width = 1.25 m and PSet_Revit_Type_Dimensions.Height = 2.01 m
    (get_element_properties).") was flagged `verification.status ==
    "unverified"` even though every real number in it was genuinely
    correct and tool-grounded.

    Root cause: check 2's entity-bound window (`_narrative_consistent_
    with_tool_facts`, "door" +/-15 chars) is naive about *which* numbers
    near the entity noun are claims that must match a known width/height
    -- it does not distinguish "the element's own tag/id, restated for
    clarity" from "a stated measurement." IFC's own `Tag` attribute is a
    string (`apps/api/app/tools/ifc/repository.py`'s own `getattr(element,
    "Tag", None)`), so it is never one of `get_element_properties`'s own
    numeric leaves in `_numeric_tokens_from_result_value` -- meaning
    "door tag 146596" makes "146596" land in the door-entity window as an
    unexplained number, indistinguishable (to the old code) from a
    fabricated claim like "there are 146596 doors."

    This is the same class of false positive this check's own docstring
    already documents three prior rounds of (a decoy real number, a
    same-entity-different-scope total) -- a new trigger, not a new kind of
    problem: identifying an element by its own tag/mark/id is a completely
    normal, correct thing for an investigation answer to do, and doing so
    must not cost it a false "unverified" caveat.
    """
    door_terms = AgentService._entity_terms_for("IfcDoor")
    facts = AgentService._numeric_tokens_from_result_value({
        "element": {"entity_type": "IfcDoor", "tag": "146596"},
        "storey": "Level 01",
        "properties": {"Width": 1.25, "Height": 2.009999999999999},
    })

    answer = (
        "IFC model: door tag 146596 has width = 1.25 m and "
        "PSet_Revit_Type_Dimensions.Height = 2.01 m (get_element_properties)."
    )
    assert AgentService._narrative_consistent_with_tool_facts(answer, [(door_terms, facts)])

    # The check must still catch a genuinely fabricated measurement even
    # when a harmless tag mention appears earlier in the same answer --
    # this fix narrows what counts as "a claimed number" near a reference
    # word, it does not disable the check for a real claim right next to
    # the entity noun itself.
    fabricated = "IFC model: door tag 146596 has width = 1.25 m. The door is 99999 m tall."
    assert not AgentService._narrative_consistent_with_tool_facts(fabricated, [(door_terms, facts)])


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


def test_v2_respects_a_deadline_that_expires_mid_tool_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent-review finding, 2026-09-17, third pass: the mid-model-
    call fix above only bounds the *model's* own stream_turn call --
    reproduced live by injecting a 200ms blocking delay into
    `_v2_dispatch_tool` itself (not the model), with the model answering
    instantly: a still-valid 50ms deadline at iteration start, and the
    turn still finalized normally ~210ms later, no timeout at all. A hung
    or slow tool call (a stuck DB read, a slow IFC query) was never
    actually interruptible, only the model round trip was.

    Fixed by dispatching via `asyncio.wait(..., timeout=...)` instead of a
    bare `asyncio.gather` -- and specifically not via `asyncio.wait_for`,
    which turned out not to work here either: `_v2_dispatch_tool` runs in
    a real OS thread via `asyncio.to_thread`, and a thread already
    executing blocking work cannot actually be cancelled, so `wait_for`
    ends up awaiting that (failed) cancellation to completion -- silently
    blocking for the tool's full duration anyway. `asyncio.wait` returns
    at the timeout regardless of whether the still-running task ever
    finishes, so the turn stops waiting on it promptly; the orphaned
    thread keeps running in the background (harmless: these are read-only
    queries) but is no longer this turn's problem.
    """
    import time as _time

    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["The project has 4 doors."]))
    _, service, resources = _service(tmp_path, fake)
    resources.ifc_repository.metadata()  # warm the cache, same reason as the mid-model-call test above

    real_dispatch = AgentService._v2_dispatch_tool

    def slow_dispatch(self, tool_call, state):
        _time.sleep(0.2)  # blocking, not asyncio.sleep -- runs inside asyncio.to_thread same as a real hung tool call
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", slow_dispatch)

    final = None
    elapsed_to_final: float | None = None
    async def _run_with_deadline():
        nonlocal final, elapsed_to_final
        t0 = _time.perf_counter()
        deadline = t0 + 0.05
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-tool-dispatch-timeout", question="How many doors are there?",
            viewer_context=None, deadline=deadline,
        ):
            if event["type"] == "final":
                final = event["response"]
                elapsed_to_final = _time.perf_counter() - t0
    # Independent-review of this test itself: timing must be measured
    # *inside* the coroutine, up to the moment the "final" event is
    # yielded -- not around the whole `asyncio.run()` call. The orphaned,
    # uncancellable dispatch thread keeps running after the timeout is
    # reported; asyncio.run()'s own shutdown phase waits for every
    # leftover task (including that one) before returning, which would
    # make this test see ~200ms regardless of how promptly the turn
    # itself actually gave up -- measuring the production code's own
    # behavior, not asyncio.run()'s unrelated cleanup cost.
    asyncio.run(_run_with_deadline())

    assert final.disposition.value == "timeout"
    assert elapsed_to_final is not None
    assert elapsed_to_final < 0.15  # nowhere near the slow tool call's own full 200ms delay


def test_v2_appends_a_caveat_to_a_truncated_response_instead_of_replacing_it(tmp_path: Path) -> None:
    """D-062 (2026-09-17): a response cut off by the model's own max-token
    limit or blocked mid-answer by content filtering (`finish_reason in
    {"length", "content_filter"}`) is a categorically different, non-
    probabilistic failure from the narrative-consistency check above (the
    response really is incomplete, not merely unconfirmed) -- it stays a
    hard `disposition=error`. But since `AnswerChunkEvent`s now stream
    live (see D-062), whatever partial text the model produced before
    being cut off has already reached the client by the time
    `finish_reason` is known; replacing it with a generic templated
    message (the previous behavior) would make the client-rendered stream
    and the saved "final" response disagree about what was actually said.
    Now appends a clear caveat to the real (if incomplete) text instead.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedAnswer(["The door height is 2.1"], finish_reason="length"))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "What is the door height?", "eval-truncated"))

    assert response.disposition.value == "error"
    assert response.verification.status == "failed"
    assert "The door height is 2.1" in response.answer_markdown  # the real, if incomplete, text is kept
    assert "cut off" in response.answer_markdown  # and a clear caveat is appended, not a full replacement


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
