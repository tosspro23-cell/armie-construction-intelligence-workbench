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
from app.agent.graph import (
    _LARGE_LIST_SAMPLE_SIZE,
    _PROPERTY_SAMPLE_CAP,
    _V2_TOOL_RESULT_LIST_CAP,
    AgentService,
)
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

    SPEC-M18 (D-074): this allowance is preserved under the structural
    redesign (see `invoke_v2`'s own "scalar_values"/"total_of_scalars"
    comment) -- the model declares the derived total under a general
    entity ('total'), which the check falls back to a shared pool for
    since it isn't a specific IFC type, and that pool now includes this
    turn's own real scalar total.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "total", "measure": "count", "value": 8}]})]))
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
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "total", "measure": "count", "value": 999}]})]))
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

    SPEC-M18 (D-074): the check itself is now structural, not a text scan
    -- the fabricated claim is scripted explicitly (a model whose own
    `submit_answer_facts` call states the same wrong number it goes on to
    narrate) rather than relying on scanning "99999" out of the prose.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 99999}]})]))
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

    SPEC-M18 (D-074): "decoy" no longer describes anything meaningful
    about this check -- it never scans the prose at all, so a real number
    sitting anywhere nearby cannot rescue a claim that doesn't itself
    match. Scripted here as a model whose own `submit_answer_facts` claim
    states the fabricated 99999 for IfcDoor's count, proving the mismatch
    is still caught even though a real "4" also appears in the text.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 99999}]})]))
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

    SPEC-M18 (D-074): the entire concept of a "window" (character
    proximity between a number and an entity noun) is retired -- there is
    no text position left to have a bug in. The same underlying claim
    (a fabricated number is still caught no matter how close a real one
    sits to it in the prose) is preserved by construction: the check
    never reads the prose to begin with, only the model's own structured
    claim.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 99999}]})]))
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

    SPEC-M18 (D-074): "merging by shared entity terms" happens by
    construction now, not as a special case -- both calls bucket under the
    same `("entity", "ifcdoor")` key in `real_facts`, so a claim for either
    scope's own real value matches. Scripted with two separate claims (one
    per scope) rather than relying on the old entity-window scan.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcDoor", "storey": "Level 01"}),
    ]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcDoor", "measure": "count", "value": 4},
        {"entity": "IfcDoor", "measure": "count", "value": 2},
    ]})]))
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
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcDoor", "measure": "count", "value": 4},
        {"entity": "IfcDoor", "measure": "count", "value": 99999},
    ]})]))
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

    SPEC-M18 (D-074): the fabricated height is now the model's own
    declared claim, checked against IfcDoor's real bucket (populated from
    `get_element_properties`'s real numeric leaves) directly.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "height_m", "value": 99999}]})]))
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


def test_v2_caps_each_sample_items_own_properties_when_the_list_is_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-067 (2026-09-21), found live via an owner-requested stress test of
    the agent-investigation feature against the real "RWTH DigitalHub"
    building: `_V2_TOOL_RESULT_LIST_CAP` (above) bounds how many *items* a
    large list sends back, but not how much each item's own `properties`
    dict costs. Measured directly against the real IFC file: a single
    `get_element_properties(entity_type="IfcDoor")` call, already capped to
    40 items, still cost ~2.2KB/item (~22K tokens for 40 items) because a
    real, professionally-authored Revit export attaches dozens of vendor-
    specific parameters to every element -- unlike this project's own small
    synthetic fixtures. The live investigation that reproduced a persistent
    real Azure OpenAI 429 called this tool twice in one turn (once for
    doors, once for windows); together, comfortably over the deployment's
    per-request token budget regardless of how long a retry waited.

    Verified here with a synthetic 45-item list (over `_V2_TOOL_RESULT_LIST_
    CAP`) where each item carries 30 properties (more than `_PROPERTY_
    SAMPLE_CAP`) -- asserts each sample item's own `properties` dict is
    bounded with a disclosed `properties_omitted_count`, that the untruncated
    field this test's own question needs (Height) survives the trim (kept
    because the trim is alphabetically stable, not because it was singled
    out as "relevant" -- see `_cap_item_properties`'s own docstring for why
    this cap deliberately does not try to guess relevance), and that
    `distinct_value_summary` -- computed from the full, untruncated list --
    is unaffected by the new per-item trim.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 2 distinct heights: 2.1 m and 2.4 m."]))
    _, service, resources = _service(tmp_path, fake)

    items = []
    for i in range(45):
        height = 2.1 if i < 40 else 2.4  # a 2nd distinct value only past the 40-item sample cap
        # "Height" sorts alphabetically before "ZVendorParam*" -- included
        # in the 15-key slice not because the cap knows it's "relevant"
        # (it deliberately doesn't try, see _cap_item_properties's own
        # docstring) but because a stable alphabetical slice is
        # deterministic, unlike relying on dict insertion/iteration order.
        properties = {f"ZVendorParam{n:02d}": f"value{n}" for n in range(29)}
        properties["Height"] = height
        items.append({"element": {"express_id": 1000 + i, "entity_type": "IfcDoor", "tag": str(2000 + i)}, "storey": "Level 01", "properties": properties})

    real_dispatch = AgentService._v2_dispatch_tool

    def synthetic_large_property_rich_dispatch(self, tool_call, state):
        if tool_call.tool_name == "get_element_properties":
            return {
                "tool_result": {"answer": "Found 45 matching elements.", "disposition": "answered", "citations": [], "verification": VerificationStatus(status="verified", reason="test").model_dump(), "result_value": items},
                "evidence": [], "tool_call_delta": 1, "plan": [{"entity_type": "IfcDoor", "source": "ifc"}],
            }
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", synthetic_large_property_rich_dispatch)

    response = asyncio.run(_run(service, resources, "How many distinct heights of doors are there?", "eval-property-sample-cap"))

    assert response.disposition.value == "answered"
    second_call_messages = fake.calls[-1].prompt
    tool_message_start = second_call_messages.index("'role': 'tool'")
    tool_payload_text = second_call_messages[tool_message_start:]

    # The exhaustive, full-list-computed summary must still see both
    # distinct heights, including the one only present past index 40.
    assert "distinct_value_summary" in tool_payload_text
    assert "2.4" in tool_payload_text

    # Each sample item's own properties dict is capped and discloses how
    # much was omitted -- not silently truncated.
    assert "properties_omitted_count" in tool_payload_text
    assert tool_payload_text.count("ZVendorParam") <= 40 * _PROPERTY_SAMPLE_CAP  # bounded, not one full dump per item

    # A field this test's own question needs survives the alphabetical
    # slice -- the cap is not blind to every real field.
    assert "'Height'" in tool_payload_text or '"Height"' in tool_payload_text


def test_v2_shrinks_the_large_list_sample_size_independently_of_the_trigger_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-067 follow-up (2026-09-21): the property-cap fix above (verified
    against a single `get_element_properties` call) was NOT enough on its
    own -- a live retest minutes after deploying it reproduced the exact
    same real Azure OpenAI 429 again on the exact same real building.
    Measured directly against the real `DigitalHub_FM-ARC_v2.ifc` file
    post-fix: `_V2_TOOL_RESULT_LIST_CAP` (40) sample items, each already
    trimmed to `_PROPERTY_SAMPLE_CAP` properties, still cost ~40-42KB
    (~10K tokens) *per call*, because each item's own fixed metadata
    (global_id, express_id, a long Revit-style `name`, the JSON key names
    themselves) is a real cost the per-property trim never touched. Two
    such calls in one turn (doors, windows -- the exact live-reproduced
    pattern) still totaled ~20K tokens.

    `total_count`/`distinct_value_summary` already carry the turn's own
    exhaustive, ungeussed-at facts regardless of how many raw items are
    sampled, so `_LARGE_LIST_SAMPLE_SIZE` (10) shrinks the actual sample
    size independently of `_V2_TOOL_RESULT_LIST_CAP` (which stays the
    *trigger* threshold, unchanged -- a list of 24-40 items, like Duplex's
    real IfcWindow count, still never engages this path at all). Verified
    here with the same 45-item, 30-properties-per-item synthetic shape as
    the test above: asserts at most `_LARGE_LIST_SAMPLE_SIZE` items are
    actually sent, not `_V2_TOOL_RESULT_LIST_CAP`.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 45 doors."]))
    _, service, resources = _service(tmp_path, fake)

    items = []
    for i in range(45):
        properties = {f"ZVendorParam{n:02d}": f"value{n}" for n in range(29)}
        properties["Height"] = 2.1
        # A distinct, greppable tag per item so the actual sample count
        # sent to the model can be counted directly from the payload text.
        items.append({"element": {"express_id": 1000 + i, "entity_type": "IfcDoor", "tag": f"UNIQUEDOORTAG{i:03d}"}, "storey": "Level 01", "properties": properties})

    real_dispatch = AgentService._v2_dispatch_tool

    def synthetic_large_property_rich_dispatch(self, tool_call, state):
        if tool_call.tool_name == "get_element_properties":
            return {
                "tool_result": {"answer": "Found 45 matching elements.", "disposition": "answered", "citations": [], "verification": VerificationStatus(status="verified", reason="test").model_dump(), "result_value": items},
                "evidence": [], "tool_call_delta": 1, "plan": [{"entity_type": "IfcDoor", "source": "ifc"}],
            }
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", synthetic_large_property_rich_dispatch)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-large-list-sample-size"))

    assert response.disposition.value == "answered"
    second_call_messages = fake.calls[-1].prompt
    tool_message_start = second_call_messages.index("'role': 'tool'")
    tool_payload_text = second_call_messages[tool_message_start:]

    sent_item_count = tool_payload_text.count("UNIQUEDOORTAG")
    assert sent_item_count <= _LARGE_LIST_SAMPLE_SIZE
    assert sent_item_count < _V2_TOOL_RESULT_LIST_CAP  # the actual regression: this used to equal the (much larger) trigger cap
    # The exhaustive total is still exact even though far fewer raw items were sampled.
    assert "\"total_count\": 45" in tool_payload_text or "'total_count': 45" in tool_payload_text


def test_answer_facts_verified_recognizes_a_lists_own_length_as_a_real_fact() -> None:
    """D-068 (2026-09-21, original bug), rewritten for SPEC-M18: a fully
    correct `get_element_properties(entity_type="IfcDoor")` investigation
    answer restating "total_count: 50 doors in the IFC" (the tool's own
    real, verified total match count) used to be flagged unverified,
    because a list's own length was only ever recorded as a real fact
    inside the `reconcile_doors_windows`-specific branch of
    `_numeric_tokens_from_result_value`, never for any other list shape.
    That extraction bug is unrelated to and unfixed-by this redesign (it
    lives in `_numeric_tokens_from_result_value`, which SPEC-M18 keeps
    unchanged) -- what this test now verifies is that the *consumer* of
    that fact (`_answer_facts_verified`, replacing the retired character-
    proximity scan) correctly checks a claimed count against it, bucketed
    by entity via `_fact_bucket_key` and by measure via
    `_labeled_facts_from_tool_result` (P1 follow-up, Codex review, PR #53),
    exactly as the real fact-collection loop in `invoke_v2` does.
    """
    items = [{"element": {"express_id": 1000 + i, "entity_type": "IfcDoor", "tag": str(2000 + i)}, "storey": "Level 01", "properties": {"Height": 2.045}} for i in range(50)]
    labeled = AgentService._labeled_facts_from_tool_result("get_element_properties", {"entity_type": "IfcDoor"}, items)
    real_facts = {AgentService._fact_bucket_key({"entity_type": "IfcDoor"}): labeled}

    verified, _ = AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 50}], real_facts)
    assert verified

    # A genuinely fabricated count must still be caught.
    verified, reason = AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 99999}], real_facts)
    assert not verified
    assert "IfcDoor" in reason


def test_answer_facts_verified_is_structurally_unaffected_by_a_globalid_in_the_answer() -> None:
    """D-073 (2026-09-22, original bug): a fully correct
    `get_element_properties(entity_type=IfcWindow, tags=["2543664"])`
    answer restating the element's own real GlobalId ("GlobalId:
    0ehNcYPbH3JQicvZQLHP24") used to be flagged unverified, because the
    retired check's regex extracted "24" out of the middle of the GlobalId
    string and found no matching IfcWindow fact for it.

    SPEC-M18 (D-074): this bug class is now impossible by construction,
    not by one more special case -- `_answer_facts_verified` takes no
    answer text at all, only the model's own structured claims, so a
    GlobalId (or any other non-numeric token) embedded in the prose is
    never examined in the first place. Verified here by checking the same
    real width/height claims a correct answer would submit, independent
    of whatever the answer's own prose says about GlobalIds.
    """
    real_facts = {AgentService._fact_bucket_key({"entity_type": "IfcWindow"}): {"width": {6.0}, "height": {1.5}}}

    verified, _ = AgentService._answer_facts_verified(
        [{"entity": "IfcWindow", "measure": "width_m", "value": 6.0}, {"entity": "IfcWindow", "measure": "height_m", "value": 1.5}],
        real_facts,
    )
    assert verified

    # A genuinely fabricated width must still be caught.
    verified, reason = AgentService._answer_facts_verified([{"entity": "IfcWindow", "measure": "width_m", "value": 99999}], real_facts)
    assert not verified
    assert "IfcWindow" in reason


def test_answer_facts_verified_binds_claims_to_their_own_declared_entity() -> None:
    """SPEC-M18 (D-074)'s own trigger, found live 2026-09-25: "There are 14
    doors and 24 windows in the building." -- both numbers real and
    correct (`count_elements(IfcDoor)` = 14, `count_elements(IfcWindow)` =
    24) -- was flagged unverified under the retired check, because "24"
    (windows' own real count) sat within the 15-character scan window
    around "doors", and door's own expected set was only {14}. This is not
    an edge case: reporting two different entities' counts in one ordinary
    sentence is the single most natural way to answer a two-part question.

    The redesign closes this at the root: each claim is bound to its own
    declared entity, so a door claim can only ever be checked against
    IfcDoor's own real facts, never IfcWindow's -- there is no shared
    "window" of text for two entities' numbers to collide in.
    """
    real_facts = {
        AgentService._fact_bucket_key({"entity_type": "IfcDoor"}): {"count": {14.0}},
        AgentService._fact_bucket_key({"entity_type": "IfcWindow"}): {"count": {24.0}},
    }

    verified, _ = AgentService._answer_facts_verified(
        [{"entity": "IfcDoor", "measure": "count", "value": 14}, {"entity": "IfcWindow", "measure": "count", "value": 24}],
        real_facts,
    )
    assert verified

    # A genuine cross-entity error (door's own count claimed as 24, the
    # window's real value) is still caught -- binding by entity cuts both
    # ways, it does not just widen what any claim is allowed to match.
    verified, reason = AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 24}], real_facts)
    assert not verified
    assert "IfcDoor" in reason


def test_v2_correctly_verifies_two_different_entity_counts_in_one_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end reproduction of the exact D-074 trigger, live 2026-09-25:
    "There are 14 doors and 24 windows in the building." (both real,
    correct counts) was flagged unverified because "24" (windows' own real
    count) fell within the retired check's 15-character scan window around
    "doors", whose own expected set was only {14}. Distinct counts (not
    the demo fixture's own real 4/4) are needed to actually exercise this
    -- monkeypatched here to mirror the real live values precisely (see
    `test_answer_facts_verified_binds_claims_to_their_own_declared_entity`
    for the pure unit-level version of the same claim).

    This test is proven to fail against the pre-SPEC-M18 verification code
    (commit ce77f21, which had `submit_answer_facts` wired up but still
    used the retired character-proximity scan) and to pass against the
    current code -- see docs/decisions/README.md D-074 for that fail-
    before/pass-after record.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcDoor", "measure": "count", "value": 14},
        {"entity": "IfcWindow", "measure": "count", "value": 24},
    ]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 14 doors and 24 windows in the building."]))
    _, service, resources = _service(tmp_path, fake)

    real_dispatch = AgentService._v2_dispatch_tool
    test_citation = {"evidence_id": "test-citation", "source_type": "ifc", "label": "test", "locator": {}, "project_id": "demo", "source_set_id": "demo-v1", "source_file": "test.ifc"}

    def dispatch_with_real_live_counts(self, tool_call, state):
        if tool_call.tool_name == "count_elements":
            entity_type = tool_call.arguments["entity_type"]
            count = 14 if entity_type == "IfcDoor" else 24
            return {
                "tool_result": {"answer": f"{count} {entity_type} elements.", "disposition": "answered", "citations": [test_citation], "verification": VerificationStatus(status="verified", reason="test").model_dump(), "result_value": count},
                "evidence": [], "tool_call_delta": 1, "plan": [{"entity_type": entity_type, "source": "ifc"}],
            }
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", dispatch_with_real_live_counts)

    response = asyncio.run(_run(service, resources, "How many doors and windows are there?", "eval-two-entity-counts"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"


def test_v2_exempts_a_restated_storey_name_as_a_known_reference_number(tmp_path: Path) -> None:
    """Owner-reported, 2026-09-30, found live: "第二层有几扇门?" ("how many
    doors on the second floor?") -> "第二层（Level 2）共有 8 扇门。" was
    flagged unverified, with the specific reason "The answer states 2,
    which was never submitted as a claim and does not match any value
    this turn's tool calls returned" -- the "2" in question was not the
    real door count (correctly submitted as its own claim), it was the
    model restating the storey's own name, "Level 2", for clarity.
    `_unclaimed_numbers_in_answer` (D-074's own coarse safety net) scans
    the full answer text for every number and had no way to recognize a
    storey name's own embedded digit as anything other than an
    unaccounted-for claim.

    Exactly the same class of false positive `known_reference_numbers`
    already exists to close for a restated tag/mark (D-065/D-066/D-068) --
    a storey name is the same kind of identifying label, not a
    measurement, and every citation this turn already carries its own
    real storey name in `locator["storey"]`. Verified here with the real
    demo fixture's own `count_elements(IfcDoor, storey="Level 01")`
    (already established elsewhere in this file to return 2) -- the
    answer restates "Level 01" (embedding "01") right next to the real,
    correctly-claimed count "2", so both a genuinely fabricated storey
    reference and a genuinely fabricated count would still need to be
    caught independently, not just "some number matched something."
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor", "storey": "Level 01"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 2}]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Level 01 has 2 doors."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are on Level 01?", "eval-storey-restatement"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"

    # A genuinely fabricated count must still be caught even with a real
    # storey name sitting right next to it in the same sentence.
    fake_bad = FakeModelProvider()
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor", "storey": "Level 01"})]))
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 99999}]})]))
    fake_bad.script("v2_tool_turn", ScriptedAnswer(["Level 01 has 99999 doors."]))
    _, service_bad, resources_bad = _service(tmp_path, fake_bad)
    response_bad = asyncio.run(_run(service_bad, resources_bad, "How many doors are on Level 01?", "eval-storey-restatement-bad"))
    assert response_bad.verification.status == "unverified"


def test_v2_storey_exemption_does_not_let_a_fabricated_structured_claim_through(tmp_path: Path) -> None:
    """Codex review, PR #62, P1: the storey-exemption fix above added a
    storey name's own digit to the *same* `known_reference_numbers` set
    `_answer_facts_verified` checks unconditionally for every submitted
    claim -- so a fabricated `submit_answer_facts` claim whose value
    happens to equal a storey digit (small, common numbers like 1/2) would
    have been accepted as "verified" purely because this turn's citations
    carry a "Level 02" storey name, regardless of what the claim actually
    declared. A tag/record/mark is safe to exempt unconditionally because a
    model would never coincide a real element identifier with a fabricated
    measurement's value; a storey number has no such property. Fixed by
    keeping the storey digits in their own set, merged into
    `known_reference_numbers` only for the coarser `_unclaimed_numbers_in_
    answer` safety net, never passed to `_answer_facts_verified`.

    Uses the real demo fixture's own Level 02 (2 real windows, per
    test_public_workspace.py's own established grouped-count fixture
    data) -- the correct count claim must still verify; the fabricated
    width_m=2 claim (no real window width in this fixture is exactly 2 m)
    must not be waved through just because "02" sits in the storey name.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcWindow", "storey": "Level 02"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcWindow", "measure": "count", "value": 2},
        {"entity": "IfcWindow", "measure": "width_m", "value": 2},  # fabricated -- no real window is 2m wide
    ]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Level 02 has 2 windows, each 2m wide."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many windows are on Level 02 and how wide are they?", "eval-storey-claim-fabrication"))

    assert response.verification.status == "unverified"
    assert "2" in response.verification.reason  # names the fabricated width_m value, not a generic message


def test_v2_exempts_a_tag_cited_only_via_reconciliations_pdf_side_mark_locator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-068 follow-up, found reading the code while investigating the bug
    above: `known_reference_numbers` (D-066) only ever checked citation
    locators for a `"tag"` or `"record"` key -- missing `reconcile_doors_
    windows`'s own PDF-side citation locator key, `"mark"`
    (`_synthesize_reconciliation_response`'s `{"page": 2, "mark": tag}`,
    see graph.py around its `_citations` call). A finding whose tag is
    genuinely absent from the IFC side (`missing_in_ifc` -- this exact
    investigated finding's own case on the real DigitalHub building) has
    *only* a PDF-side citation, so its own tag number was never being
    exempted at all, regardless of phrasing, before this fix.

    Verified end-to-end through `invoke_v2` so the actual citation-scanning
    code (which builds `known_reference_numbers`) is exercised, not just
    its consumer. SPEC-M18 (D-074): the identifier is submitted as its own
    claim here to exercise the `known_reference_numbers` safety net inside
    `_answer_facts_verified` directly, simulating a model that restates an
    identifier as a claim despite `submit_answer_facts`'s own instruction
    not to -- proving the exemption still holds even then.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcDoor", "measure": "count", "value": 4},
        {"entity": "IfcDoor", "measure": "tag", "value": 999999},
    ]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 4 doors, and door 999999 does not appear in the IFC model at all."]))
    _, service, resources = _service(tmp_path, fake)

    real_dispatch = AgentService._v2_dispatch_tool

    def dispatch_with_mark_only_citation(self, tool_call, state):
        result = real_dispatch(self, tool_call, state)
        result["tool_result"]["citations"] = [
            *result["tool_result"].get("citations", []),
            {"evidence_id": "test-mark-citation", "source_type": "pdf", "label": "PDF schedule row Mark=999999.", "locator": {"page": 2, "mark": "999999"}, "project_id": "demo", "source_set_id": "demo-v1", "source_file": "test.pdf"},
        ]
        return result

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", dispatch_with_mark_only_citation)

    response = asyncio.run(_run(service, resources, "How many doors are there, and is any tag missing from the IFC?", "eval-mark-only-citation"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"  # not "unverified" -- the mark-cited tag is a real, legitimate restatement


def test_v2_recognizes_a_distinct_value_summarys_own_counts_as_real_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-069 (2026-09-21), found live minutes after redeploying D-068 on the
    same real DigitalHub building, same finding: a fully correct answer
    restating "Qto_DoorBaseQuantities.Width = 1.09 for 39 doors" -- a real,
    exact count taken straight from `distinct_value_summary` (itself
    computed from the full, real 50-door list) -- was flagged unverified.

    Root cause: `facts` (this call's own recognized numeric facts) is
    computed from the *raw* tool result, before `distinct_value_summary`
    is even built for the model-facing payload -- so a per-value count that
    only exists inside that summary (not as any single item's own leaf
    value) was never a recognized fact, even though the model is
    specifically told to use `distinct_value_summary` for exactly this
    kind of question (see its own "note" field, D-067/D-068's `_V2_TOOL_
    RESULT_LIST_CAP` branch).

    Verified here with a synthetic 45-item list (over `_V2_TOOL_RESULT_
    LIST_CAP`) where exactly 39 items share one height value -- asserts an
    answer restating that real count is verified, and a fabricated count
    for the same field is still caught.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [
        {"entity": "IfcDoor", "measure": "count", "value": 39},
        {"entity": "IfcDoor", "measure": "count", "value": 6},
    ]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Height = 2.045 for 39 doors, and Height = 2.25 for the remaining 6 doors."]))
    _, service, resources = _service(tmp_path, fake)

    items = []
    for i in range(45):
        height = 2.045 if i < 39 else 2.25  # exactly 39 items share this value -- not derivable from any single item alone
        items.append({"element": {"express_id": 1000 + i, "entity_type": "IfcDoor", "tag": str(2000 + i)}, "storey": "Level 01", "properties": {"Height": height}})

    real_dispatch = AgentService._v2_dispatch_tool

    test_citation = {"evidence_id": "test-citation", "source_type": "ifc", "label": "test", "locator": {}, "project_id": "demo", "source_set_id": "demo-v1", "source_file": "test.ifc"}

    def synthetic_large_list_dispatch(self, tool_call, state):
        if tool_call.tool_name == "get_element_properties":
            return {
                "tool_result": {"answer": "Found 45 matching elements.", "disposition": "answered", "citations": [test_citation], "verification": VerificationStatus(status="verified", reason="test").model_dump(), "result_value": items},
                "evidence": [], "tool_call_delta": 1, "plan": [{"entity_type": "IfcDoor", "source": "ifc"}],
            }
        return real_dispatch(self, tool_call, state)

    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", synthetic_large_list_dispatch)

    response = asyncio.run(_run(service, resources, "How many doors have each height value?", "eval-distinct-value-count-fact"))

    assert response.disposition.value == "answered"
    assert response.verification.status == "verified"  # the real 39/6 split, straight from distinct_value_summary, is not fabrication

    # A fabricated count for the same field must still be caught.
    fake_bad = FakeModelProvider()
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcDoor"})]))
    fake_bad.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 99999}]})]))
    fake_bad.script("v2_tool_turn", ScriptedAnswer(["Height = 2.045 for 99999 doors."]))
    _, service_bad, resources_bad = _service(tmp_path, fake_bad)
    monkeypatch.setattr(AgentService, "_v2_dispatch_tool", synthetic_large_list_dispatch)
    response_bad = asyncio.run(_run(service_bad, resources_bad, "How many doors have each height value?", "eval-distinct-value-count-fact-bad"))
    assert response_bad.verification.status == "unverified"


def test_answer_facts_verified_tolerates_natural_rounding_of_a_measurement() -> None:
    """Owner-reported, 2026-09-17 (found live): a real space_distance call
    returning {"value_m": 5.234, ...} (the same shape aggregate_quantity
    uses) was phrased by the model as "5.23 m" -- a natural, honest
    rounding choice, not fabrication. A count (whole number) still
    requires an exact match: "4 doors" vs "5 doors" is a real discrepancy,
    not rounding. Both tolerance rules are unchanged by SPEC-M18 --
    `_answer_facts_verified` reuses the same `_matches` rule the retired
    check used, applied to a declared claim instead of a text-scanned one.
    """
    real_facts = {AgentService._fact_bucket_key({"entity_type": "IfcSpace"}): AgentService._labeled_facts_from_tool_result("space_distance", {}, {"value_m": 5.234, "from_space": "B204", "to_space": "B202"})}
    assert AgentService._answer_facts_verified([{"entity": "IfcSpace", "measure": "distance_m", "value": 5.23}], real_facts)[0]
    assert AgentService._answer_facts_verified([{"entity": "IfcSpace", "measure": "distance_m", "value": 5.2}], real_facts)[0]
    assert AgentService._answer_facts_verified([{"entity": "IfcSpace", "measure": "distance_m", "value": 5.234}], real_facts)[0]
    assert not AgentService._answer_facts_verified([{"entity": "IfcSpace", "measure": "distance_m", "value": 12}], real_facts)[0]

    door_facts = {AgentService._fact_bucket_key({"entity_type": "IfcDoor"}): AgentService._labeled_facts_from_tool_result("count_elements", {}, 4)}
    assert AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 4}], door_facts)[0]
    assert not AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 5}], door_facts)[0]


def test_answer_facts_verified_exempts_a_restated_reference_number() -> None:
    """D-065/D-066 (2026-09-20), consolidated for SPEC-M18: the retired
    check's own bugs here (a restated tag with no preceding reference
    word, a plural "tags X and Y" restatement, and a fixed-width scan
    window bisecting a real number sitting at its edge) were all, at
    root, about *locating* a claim in free text near an entity noun.
    That entire mechanism -- text position, scan windows, word-boundary
    regexes -- is retired under SPEC-M18: `_answer_facts_verified` takes
    no answer text, so there is no position left to have a bug in.

    The actual guarantee those fixes protected -- restating an element's
    own real tag/mark, however phrased, must never be mistaken for a
    fabricated measurement -- is preserved via the `known_reference_
    numbers` safety net, tested here directly: a claim whose value is a
    known reference number is exempted regardless of what entity/measure
    it's declared under, since it wouldn't otherwise match that entity's
    own real measurement facts.
    """
    real_facts = {AgentService._fact_bucket_key({"entity_type": "IfcDoor"}): {"width": {1.25}, "height": {2.01}}}
    known_reference_numbers = {146596.0, 146678.0, 146600.0}

    # Neither restated tag is a real IfcDoor width/height -- without the
    # exemption, both would incorrectly read as fabricated measurements.
    verified, _ = AgentService._answer_facts_verified(
        [
            {"entity": "IfcDoor", "measure": "width_m", "value": 1.25},
            {"entity": "IfcDoor", "measure": "height_m", "value": 2.01},
            {"entity": "IfcDoor", "measure": "tag", "value": 146596},
            {"entity": "IfcDoor", "measure": "tag", "value": 146678},
        ],
        real_facts, known_reference_numbers,
    )
    assert verified

    # A genuinely fabricated measurement submitted alongside a legitimate
    # reference-number restatement must still be caught.
    verified, reason = AgentService._answer_facts_verified(
        [{"entity": "IfcDoor", "measure": "tag", "value": 146600}, {"entity": "IfcDoor", "measure": "height_m", "value": 99999}],
        real_facts, known_reference_numbers,
    )
    assert not verified
    assert "IfcDoor" in reason


def test_answer_facts_verified_falls_back_to_a_shared_pool_for_untyped_facts() -> None:
    """SPEC-M18 (D-074): `reconcile_doors_windows`'s own per-status counts
    describe both doors and windows collectively, not one typed IFC
    entity, and carry no PDF field name either -- `_fact_bucket_key`
    assigns these to a shared "global" bucket, the same weak,
    undiscriminating guarantee the retired check gave every claim, kept
    here only as a fallback for a claim whose own entity/measure don't
    resolve to anything more specific.
    """
    real_facts = {("global", ""): {"count": {6.0, 1.0}}}
    verified, _ = AgentService._answer_facts_verified([{"entity": "reconciliation", "measure": "matched_count", "value": 6}], real_facts)
    assert verified

    verified, reason = AgentService._answer_facts_verified([{"entity": "reconciliation", "measure": "matched_count", "value": 99999}], real_facts)
    assert not verified
    assert "reconciliation" in reason


def test_answer_facts_verified_does_not_let_one_measure_validate_a_different_one() -> None:
    """Codex review, PR #53, P1: this PR's own first draft bucketed
    `real_facts` by entity only, with no measure dimension at all -- a
    claim for one measure of an entity (its height) could be validated by
    a completely different measure's real value (that same entity's own
    count) merely because both numbers shared the entity's bucket.
    Concretely: `count_elements(IfcDoor)` returns 4; a claim of {entity:
    'IfcDoor', measure: 'height_m', value: 4} would incorrectly verify,
    since 4 is a real IfcDoor fact -- just not a height.

    Fixed by tagging each real fact with the specific measure it came from
    (`_labeled_facts_from_tool_result`) and requiring a claim's own
    declared measure to match that tag (`_normalize_measure_key`), not
    just its entity.
    """
    real_facts = {AgentService._fact_bucket_key({"entity_type": "IfcDoor"}): AgentService._labeled_facts_from_tool_result("count_elements", {}, 4)}

    # The door's own real count (4) is not a real height -- must be caught.
    verified, reason = AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "height_m", "value": 4}], real_facts)
    assert not verified
    assert "IfcDoor" in reason

    # The same value, correctly declared as the entity's own count, still verifies.
    assert AgentService._answer_facts_verified([{"entity": "IfcDoor", "measure": "count", "value": 4}], real_facts)[0]


def test_v2_flags_a_number_stated_in_the_answer_but_never_submitted_as_a_claim(tmp_path: Path) -> None:
    """Codex review, PR #53, P1: `_answer_facts_verified` alone only checks
    that every *submitted* claim is real -- it never notices a number the
    answer's own prose states but the model never submitted a claim for at
    all. Reproduced live-shape: "There are 4 doors and 999 windows." with
    only the (correct) door claim submitted -- the fabricated window count
    was never checked against anything, since nothing claimed it.

    Fixed by `_unclaimed_numbers_in_answer`, a coarse safety net layered on
    top of the structural check: every number in the answer's own prose
    must correspond to *something* real or claimed, even though it wasn't
    itself submitted as a claim. Deliberately reintroduces only the
    retired check's baseline half (does this number correspond to
    something real anywhere this turn), never its entity-bound half (the
    proximity scanning that caused all eight prior false positives).
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("count_elements", {"entity_type": "IfcDoor"}),
        ("count_elements", {"entity_type": "IfcWindow"}),
    ]))
    # Only the door claim submitted -- the fabricated "999" for windows is
    # never declared as a claim at all, not even a wrong one.
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": [{"entity": "IfcDoor", "measure": "count", "value": 4}]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["There are 4 doors and 999 windows."]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors and windows are there?", "eval-unclaimed-number"))

    assert response.verification.status == "unverified"
    assert "999" in response.verification.reason


def test_v2_submit_answer_facts_alone_does_not_count_as_an_answered_subtask(tmp_path: Path) -> None:
    """Codex review, PR #53, P2: `submit_answer_facts` is a local
    bookkeeping no-op, appended to `subtask_dispositions` like any other
    tool call before this fix -- a compliant model calling it with an
    empty claims list (per its own tool description, for a no-number
    answer) before an unsupported/clarification response would make that
    bookkeeping call alone count as a successful "answered" subtask,
    masking the turn's real disposition.

    Reproduced here with a scripted turn that calls only submit_answer_facts
    (empty claims) and no real tool at all -- the turn's own `subtask_
    dispositions` list must end up empty (not `["answered"]`), so the
    existing "no tool call was dispatched" fallback (clarification_required,
    since no citations exist either) applies, not a false "answered."
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("submit_answer_facts", {"claims": []})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Could you clarify which building you mean?"]))
    _, service, resources = _service(tmp_path, fake)

    response = asyncio.run(_run(service, resources, "How many doors are there?", "eval-bookkeeping-only-disposition"))

    assert response.disposition.value == "clarification_required"


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


def test_v2_system_prompt_discloses_an_existing_viewer_selection_regardless_of_source_preference() -> None:
    """Owner-reported, 2026-10-04, found live: selected a window in the 3D
    viewer, then asked "what height is this window?" with Source left at
    its default "Auto" -- the model asked the user to re-identify a window
    it had, in fact, already been told about (the frontend always sends
    `selected_global_ids` on every V2 turn, `main.tsx`'s `runV2Turn`). The
    gap was that nothing in the system prompt ever disclosed a selection
    existed unless `source_preference` happened to be the separate,
    narrower "viewer_snapshot" value.

    A plain click-selection (no "Capture current view" screenshot) must
    route to `get_element_properties(global_ids=..., entity_type=...)` --
    the same deterministic property lookup any other question already
    uses -- never `inspect_current_view`: that tool requires a real
    screenshot (`_execute_viewer`'s own precondition) and would itself
    return a *different* clarification_required ("capture the current
    model view...") when given only a GlobalId, trading one unnecessary
    clarification for another. `viewer_guidance` now states the actual
    selection unconditionally, independent of `source_preference`.
    """
    viewer_context = {
        "selected_global_ids": ["03iwAedpj2TfaeVhYaw$rx"],
        "selected_entity_type": "IfcWindow",
        "selected_display_name": "IfcWindow: FE 3 tlg - DK-Fix im Rahmen-DK-2:6000 x 1500:2530571",
    }

    prompt_auto = AgentService._v2_system_prompt(None, "auto", viewer_context)
    assert "03iwAedpj2TfaeVhYaw$rx" in prompt_auto
    assert "IfcWindow" in prompt_auto
    assert "get_element_properties" in prompt_auto
    assert "do not ask the user to" in prompt_auto

    prompt_snapshot = AgentService._v2_system_prompt(None, "viewer_snapshot", viewer_context)
    assert "03iwAedpj2TfaeVhYaw$rx" in prompt_snapshot


def test_v2_system_prompt_routes_a_captured_snapshot_to_inspect_current_view() -> None:
    """The flip side of the test above: when a real screenshot was
    captured (`screenshot_base64` present, "Capture current view"), the
    right tool genuinely is the vision-based `inspect_current_view` --
    confirming the fix did not blanket-replace that recommendation, only
    corrected it for the plain-selection-without-a-snapshot case.
    """
    prompt = AgentService._v2_system_prompt(None, "auto", {
        "selected_global_ids": ["03iwAedpj2TfaeVhYaw$rx"], "selected_entity_type": "IfcWindow",
        "screenshot_base64": "ZmFrZS1wbmc=",
    })
    assert "inspect_current_view" in prompt
    assert "get_element_properties" in prompt  # both offered: a real GlobalId is still known too


def test_v2_system_prompt_omits_viewer_guidance_when_nothing_is_selected() -> None:
    """The flip side of the test above: no selection, no snapshot -- the
    prompt must not claim one exists (there is nothing for `inspect_
    current_view` to resolve, and claiming otherwise would just invite a
    new, different false confidence).
    """
    assert "3D viewer" not in AgentService._v2_system_prompt(None, "auto", None)
    assert "3D viewer" not in AgentService._v2_system_prompt(None, "auto", {})
    assert "3D viewer" not in AgentService._v2_system_prompt(None, "auto", {"selected_global_ids": [], "screenshot_base64": None})


def test_v2_invoke_actually_sends_the_viewer_selection_to_the_model(tmp_path: Path) -> None:
    """End-to-end confirmation that `invoke_v2` (not just `_v2_system_prompt`
    in isolation) actually wires the turn's own `viewer_context` into the
    real message sent to the model -- `FakeModelProvider` records the exact
    `messages` list `stream_turn` was called with (its own `RecordedCall.
    prompt`, `str(messages)`), so this proves the selection reaches the
    model's first turn, not merely that the helper function can produce it.

    Scripts the real demo fixture's own W01 window (global_id
    `1ctyjgDIX8IAhrrKTlC1w0`, established elsewhere in this file/
    `test_reconciliation.py`) being resolved via `get_element_properties`
    -- the tool the corrected guidance actually recommends for a plain
    selection with no captured snapshot, matching this repo's own real
    `get_element_properties(global_ids=...)` parameter shape.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcWindow", "global_ids": ["1ctyjgDIX8IAhrrKTlC1w0"]})]))
    fake.script("v2_tool_turn", ScriptedAnswer(["This window is 1.50 m tall."]))
    _, service, resources = _service(tmp_path, fake)

    final = None
    async def _run_with_selection():
        nonlocal final
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-viewer-selection", question="What height is this window?",
            viewer_context={
                "selected_global_ids": ["1ctyjgDIX8IAhrrKTlC1w0"],
                "selected_entity_type": "IfcWindow", "selected_display_name": "W01",
            },
        ):
            if event["type"] == "final":
                final = event["response"]
    asyncio.run(_run_with_selection())

    assert final.disposition.value == "answered"
    first_turn_prompt = fake.calls[0].prompt
    assert "1ctyjgDIX8IAhrrKTlC1w0" in first_turn_prompt
    assert "get_element_properties" in first_turn_prompt


def test_v2_suppresses_viewer_guidance_during_a_finding_investigation(tmp_path: Path) -> None:
    """Codex review, PR #63, P2: `build_finding_investigation_question`
    refers to the finding under investigation as "this element" throughout
    (see its own docstring), identified by the finding's own tag --
    completely independent of whatever the user happens to still have
    selected in the 3D viewer from earlier browsing. The frontend sends
    the current viewer selection on *every* V2 turn regardless of which
    finding was clicked (`main.tsx`'s `runV2Turn`), so an unrelated, stale
    selection could otherwise be sitting right there when "Ask agent to
    investigate" is clicked for a completely different finding. Without
    this guard, the viewer-selection fix above would let the model resolve
    the investigation question's own "this element" to that unrelated
    GlobalId instead of the finding actually under investigation --
    exactly the kind of correctness risk this project's own door/window
    reconciliation pilot cannot afford. `invoke_v2`'s own call site
    suppresses `viewer_context` outright (passes `None`) whenever
    `include_verdict_tool` is set, which is true if and only if this turn
    is a finding investigation (`_chat_v2`'s own exclusive setter) -- so
    the finding's own tag is the only thing "this element" can mean.

    Exercises `invoke_v2` directly (not the real `/api/v1/findings` HTTP
    flow `test_finding_verdict_tool.py` already covers end-to-end) with an
    unrelated, clearly-stale GlobalId in `viewer_context` alongside
    `include_verdict_tool=True`, and inspects the real system prompt sent
    to the model.
    """
    fake = FakeModelProvider()
    fake.script("v2_tool_turn", ScriptedToolCalls([
        ("get_element_properties", {"entity_type": "IfcWindow", "tags": ["W02"]}),
        ("submit_finding_verdict", {"verdict": "inconclusive", "basis": "Could not corroborate either value."}),
    ]))
    fake.script("v2_tool_turn", ScriptedAnswer(["Inconclusive; see my submitted verdict."]))
    _, service, resources = _service(tmp_path, fake)

    final = None
    async def _run_investigation():
        nonlocal final
        async for event in service.invoke_v2(
            project_resources=resources, thread_id="eval-investigation-stale-selection",
            question="Investigate the dimension mismatch for tag W02.",
            viewer_context={
                "selected_global_ids": ["UNRELATED-STALE-SELECTION-GLOBALID"],
                "selected_entity_type": "IfcDoor", "selected_display_name": "A totally different door",
            },
            include_verdict_tool=True,
        ):
            if event["type"] == "final":
                final = event["response"]
    asyncio.run(_run_investigation())

    assert final.disposition.value == "answered"
    first_turn_prompt = fake.calls[0].prompt
    assert "UNRELATED-STALE-SELECTION-GLOBALID" not in first_turn_prompt
    assert "do not ask the user to re-identify" not in first_turn_prompt
