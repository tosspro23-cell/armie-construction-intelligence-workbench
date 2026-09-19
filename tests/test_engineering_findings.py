"""SPEC-M11: Engineering Finding Workflow.

Covers the state machine (app/finding_workflow.py) table-driven, upsert-
not-duplicate behaviour across two reconciliation runs, and re-verify's
two real branches -- proven with a genuine fresh re-read of the sources,
not by trusting a finding's own stored values (see
AgentService.reverify_reconciliation_tag).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import get_settings
from app.finding_workflow import (
    IllegalFindingTransition,
    resolve_reverify_outcome,
    validate_transition,
)
from app.schemas.models import FindingStatus
from fakes.fake_provider import ScriptedAnswer, ScriptedToolCalls
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
RECONCILIATION_QUESTION = "Please reconcile the door and window schedule between the IFC model and the PDF drawing."


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()


# --- state machine, table-driven ---------------------------------------------

LEGAL_TRANSITIONS = [
    (FindingStatus.OPEN, "acknowledge", FindingStatus.ACKNOWLEDGED),
    (FindingStatus.ACKNOWLEDGED, "start_action", FindingStatus.ACTION_REQUIRED),
    (FindingStatus.ACKNOWLEDGED, "waive", FindingStatus.WAIVED),
    (FindingStatus.ACKNOWLEDGED, "mark_false_positive", FindingStatus.FALSE_POSITIVE),
    (FindingStatus.ACTION_REQUIRED, "resolve", FindingStatus.RESOLVED),
]


@pytest.mark.parametrize("current_status,action,expected", LEGAL_TRANSITIONS)
def test_legal_transitions_reach_the_intended_status(current_status, action, expected) -> None:
    assert validate_transition(current_status, action) == expected


ILLEGAL_TRANSITIONS = [
    (FindingStatus.OPEN, "start_action"),
    (FindingStatus.OPEN, "resolve"),
    (FindingStatus.OPEN, "waive"),
    (FindingStatus.ACKNOWLEDGED, "acknowledge"),
    (FindingStatus.ACTION_REQUIRED, "acknowledge"),
    (FindingStatus.ACTION_REQUIRED, "waive"),
    (FindingStatus.RESOLVED, "resolve"),
    (FindingStatus.RESOLVED, "acknowledge"),
    (FindingStatus.VERIFIED_CLOSED, "acknowledge"),
    (FindingStatus.WAIVED, "acknowledge"),
    (FindingStatus.FALSE_POSITIVE, "acknowledge"),
    (FindingStatus.OPEN, "not_a_real_action"),
]


@pytest.mark.parametrize("current_status,action", ILLEGAL_TRANSITIONS)
def test_illegal_transitions_are_rejected_not_silently_no_opped(current_status, action) -> None:
    with pytest.raises(IllegalFindingTransition):
        validate_transition(current_status, action)


def test_resolve_reverify_outcome_is_legal_only_from_resolved() -> None:
    for status in FindingStatus:
        if status is FindingStatus.RESOLVED:
            continue
        with pytest.raises(IllegalFindingTransition):
            resolve_reverify_outcome(status, now_matches=True)


def test_resolve_reverify_outcome_branches_on_the_fresh_read() -> None:
    assert resolve_reverify_outcome(FindingStatus.RESOLVED, now_matches=True) == FindingStatus.VERIFIED_CLOSED
    assert resolve_reverify_outcome(FindingStatus.RESOLVED, now_matches=False) == FindingStatus.ACTION_REQUIRED


# --- auto-creation + upsert-not-duplicate ------------------------------------

def test_reconciliation_creates_exactly_the_three_non_matched_findings(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        chat_response = client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        assert chat_response.status_code == 200

        findings = client.get("/api/v1/findings", params={"project_id": "demo"}).json()

    by_tag = {item["tag"]: item for item in findings}
    assert set(by_tag) == {"W05", "W02", "D04"}
    assert by_tag["W05"]["finding_type"] == "missing_in_ifc"
    assert by_tag["W02"]["finding_type"] == "dimension_mismatch"
    assert by_tag["W02"]["severity"] == "medium"
    assert by_tag["D04"]["finding_type"] == "missing_in_pdf"
    assert by_tag["D04"]["severity"] == "high"
    assert all(item["status"] == "open" for item in findings)


def test_re_running_reconciliation_updates_the_same_finding_instead_of_duplicating(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        first = client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        assert first.status_code == 200
        first_findings = client.get("/api/v1/findings", params={"project_id": "demo"}).json()
        w02_first = next(item for item in first_findings if item["tag"] == "W02")

        second = client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        assert second.status_code == 200
        second_findings = client.get("/api/v1/findings", params={"project_id": "demo"}).json()

    assert len(second_findings) == 3
    w02_second = next(item for item in second_findings if item["tag"] == "W02")
    # Same finding_id, not a fresh row -- upsert keyed on (project_id, tag,
    # finding_type), not a new insert every time reconciliation reruns.
    assert w02_second["finding_id"] == w02_first["finding_id"]


# --- transition API -----------------------------------------------------------

def test_illegal_transition_over_http_is_a_409_not_a_silent_no_op(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")

        # W02 starts OPEN -- "resolve" is only legal from ACTION_REQUIRED.
        response = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": "resolve"})
        assert response.status_code == 409

        unchanged = client.get(f"/api/v1/findings/{finding_id}").json()
        assert unchanged["status"] == "open"


def _walk_to_resolved(client, finding_id: str) -> None:
    for action in ("acknowledge", "start_action", "resolve"):
        response = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": action})
        assert response.status_code == 200, response.text


# --- re-verify: both real branches -------------------------------------------

def test_reverify_still_mismatched_bounces_back_to_action_required_with_fresh_detail(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_resolved(client, finding_id)

        # No source was actually fixed -- a genuine re-read still finds the
        # same real mismatch (IFC 1.75 vs PDF 1.70), not a mocked outcome.
        reverified = client.post(f"/api/v1/findings/{finding_id}/reverify")

    assert reverified.status_code == 200
    body = reverified.json()
    assert body["status"] == "action_required"
    assert "differ beyond" in body["detail"]
    assert body["history"][-1]["note"] == "Re-verify found the mismatch still present."


def test_reverify_now_matching_closes_the_finding(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_resolved(client, finding_id)

        # Simulate the PDF schedule having been corrected to match the real
        # IFC dimensions -- only the source-read seam is faked (the exact
        # technique tests/test_reconciliation.py's own
        # test_pdf_read_failure_is_reported_as_error_not_answered already
        # uses for this seam); the actual comparison/outcome logic under
        # test (_compare_reconciliation_item, resolve_reverify_outcome)
        # still runs for real, unmocked.
        monkeypatch.setattr(
            main_module.app.state.agent,
            "_reconciliation_pdf_items",
            lambda project_resources, page_number=2: {"W02": {"tag": "W02", "width_m": 1.2, "height_m": 1.75}},
        )
        reverified = client.post(f"/api/v1/findings/{finding_id}/reverify")

    assert reverified.status_code == 200
    body = reverified.json()
    assert body["status"] == "verified_closed"
    assert body["pdf_width_m"] == pytest.approx(1.2)
    assert body["pdf_height_m"] == pytest.approx(1.75)
    assert body["history"][-1]["note"] == "Re-verified against the current sources."


def test_reverify_is_illegal_outside_resolved(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")

        response = client.post(f"/api/v1/findings/{finding_id}/reverify")

    assert response.status_code == 409


# --- SPEC-M17: agent-assisted resolution ---------------------------------------

def _walk_to_action_required(client, finding_id: str) -> None:
    for action in ("acknowledge", "start_action"):
        response = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": action})
        assert response.status_code == 200, response.text


def _install_fake_agent(main_module, monkeypatch, chunks: list[str], tool_calls: list[tuple[str, dict]] | None = None):
    """Swaps `app.state.container`/`app.state.agent` for FakeModelProvider-
    backed instances. `tool_calls`, if given, scripts a real V2 tool-
    selection round before the final answer (so the resulting proposal
    carries real citations); omitted, the scripted answer has no tool call
    at all -- enough to exercise the finding-investigation wiring itself
    without needing evidence. Reconciliation itself (creating the findings
    these tests walk through first) makes zero model calls regardless
    (SPEC-M2, D-011) -- swapping the agent in only matters for the
    investigation turn each test actually exercises.
    """
    from app.agent.graph import AgentService
    from app.services import ServiceContainer
    from fakes.fake_provider import FakeModelProvider, ScriptedAnswer, ScriptedToolCalls

    fake = FakeModelProvider()
    if tool_calls:
        fake.script("v2_tool_turn", ScriptedToolCalls(tool_calls))
    fake.script("v2_tool_turn", ScriptedAnswer(chunks))
    container = ServiceContainer(main_module.app.state.container.settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    monkeypatch.setattr(main_module.app.state, "container", container)
    monkeypatch.setattr(main_module.app.state, "agent", AgentService(container))
    return fake


def _investigate(client, finding_id: str, thread_id: str = "findings-thread") -> dict:
    """SPEC-M17, amended 2026-09-18: "investigate this finding" is now a
    normal V2 chat turn (`ChatRequest.finding_id`), not a dedicated REST
    endpoint -- it streams into the same Conversation panel as any other
    question. `question` is still required by `ChatRequest`'s own
    validation (kept unchanged so V1's shared field is untouched) but is
    ignored server-side whenever `finding_id` is set.
    """
    response = client.post("/api/v1/chat", json={
        "question": "[finding investigation]", "engine": "v2", "thread_id": thread_id, "finding_id": finding_id,
    })
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.strip().split("\n\n") if line.startswith("data: ")]
    return {"status_code": response.status_code, "events": events}


def test_investigate_finding_is_illegal_outside_action_required(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")  # still OPEN

        result = _investigate(client, finding_id)

    assert result["status_code"] == 409


@pytest.mark.parametrize("tag,finding_type", [("D04", "missing_in_pdf"), ("W05", "missing_in_ifc")])
def test_investigate_finding_is_now_legal_for_missing_in_pdf_and_missing_in_ifc(monkeypatch, tmp_path, tag: str, finding_type: str) -> None:
    """Owner decision, 2026-09-19 (live-testing session): originally these
    two finding types were explicitly rejected with a 409 (see this
    test's own prior name/assertion in git history) -- SPEC-M17's own
    "Explicitly excluded scope" limited investigation to dimension_mismatch
    for the first pass. Live verification against the real Azure
    deployment for a dimension_mismatch finding confirmed the
    chat-embedded architecture genuinely demonstrates multi-step,
    multi-tool reasoning; the owner then asked to widen coverage to the
    other two SPEC-M11 finding types (see INVESTIGABLE_FINDING_TYPES in
    app/finding_workflow.py) rather than continuing to reject them.
    """
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        fake = _install_fake_agent(
            main_module, monkeypatch,
            [f"I re-checked tag '{tag}' with the available tools and it does not appear under a different label -- this is a genuine {finding_type.replace('_', ' ')} that needs a manual correction."],
            tool_calls=[("get_element_properties", {"entity_type": "IfcWindow"})],
        )
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == tag)
        assert next(item["finding_type"] for item in client.get("/api/v1/findings").json() if item["tag"] == tag) == finding_type
        _walk_to_action_required(client, finding_id)

        result = _investigate(client, finding_id)

    assert result["status_code"] == 200, result["events"]
    event_types = {event["type"] for event in result["events"]}
    assert "tool_status" in event_types
    assert "answer_chunk" in event_types
    final_response = next(event for event in result["events"] if event["type"] == "final")["response"]
    assert final_response["disposition"] == "answered"
    proposal = client.get(f"/api/v1/findings/{finding_id}").json()["pending_proposal"]
    assert proposal is not None
    assert fake.calls  # the fake model was actually invoked, not bypassed


def test_approve_and_reject_proposal_are_illegal_with_no_pending_proposal(monkeypatch, tmp_path) -> None:
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_action_required(client, finding_id)

        approve = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": "approve_proposal"})
        reject = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": "reject_proposal"})

    assert approve.status_code == 409
    assert reject.status_code == 409


def test_investigate_finding_streams_into_the_conversation_and_produces_a_real_proposal(monkeypatch, tmp_path) -> None:
    """Owner decision, 2026-09-18: "investigate this finding" now runs as
    a normal V2 chat turn, streaming tool_status/answer_chunk/final events
    into the same Conversation panel as any other question -- one agent,
    one place it visibly works, not a second, disconnected surface. This
    locks in both halves: the turn actually streams the same event shapes
    a normal question does (proving it reads as "the same agent working,"
    not a bespoke endpoint), and it still ends with a real, persisted
    proposal with real citations tracing to the real IFC fixture (a real
    `get_element_properties` call executes for real; only the model's own
    two responses are scripted), not an invented one.
    """
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        fake = _install_fake_agent(
            main_module, monkeypatch,
            ["The IFC model's own recorded height (1.75 m) is correct; the PDF schedule has a transcription error."],
            tool_calls=[("get_element_properties", {"entity_type": "IfcWindow"})],
        )
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_action_required(client, finding_id)

        result = _investigate(client, finding_id)

    assert result["status_code"] == 200
    event_types = {event["type"] for event in result["events"]}
    assert "tool_status" in event_types  # the real get_element_properties call is visible in the stream, not hidden
    assert "answer_chunk" in event_types  # the conclusion streams token-by-token like any other V2 answer
    final_response = next(event for event in result["events"] if event["type"] == "final")["response"]
    assert final_response["disposition"] == "answered"

    proposal = client.get(f"/api/v1/findings/{finding_id}").json()["pending_proposal"]
    assert proposal is not None
    assert proposal["proposed_height_m"] == pytest.approx(1.75)  # the real IFC value, correctly picked out of the free-text conclusion
    assert proposal["rationale"] == "The IFC model's own recorded height (1.75 m) is correct; the PDF schedule has a transcription error."
    assert len(proposal["citations"]) > 0  # real evidence from the real get_element_properties call, not invented
    assert fake.calls  # the fake model was actually invoked, not bypassed


def test_approve_proposal_history_snapshot_survives_a_later_overwriting_proposal(monkeypatch, tmp_path) -> None:
    """SPEC-M17 §4C/§8's own immutability invariant: `approve_proposal`'s
    history entry must keep the *original* approved proposal, unaffected
    by a later investigation turn that replaces the finding's live
    `pending_proposal` field with something else entirely.
    """
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module

    with TestClient(main_module.app) as client:
        # A zero-tool-call turn honestly resolves to clarification_required
        # (SPEC-M16's own dead-code-disposition fix), which correctly has
        # no proposal to persist -- a real tool call is scripted here so
        # this test actually reaches disposition=answered, matching what a
        # real investigation always does per its own system prompt.
        fake = _install_fake_agent(
            main_module, monkeypatch, ["The IFC value 1.75 m is correct."],
            tool_calls=[("get_element_properties", {"entity_type": "IfcWindow"})],
        )
        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_action_required(client, finding_id)

        assert _investigate(client, finding_id)["status_code"] == 200
        approved = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": "approve_proposal"})
        assert approved.status_code == 200
        approved_rationale = approved.json()["history"][-1]["proposal_snapshot"]["rationale"]

        # Approving moved the finding to RESOLVED, out of ACTION_REQUIRED --
        # bounce it back via reverify (the real mismatch is still present,
        # nothing was actually fixed) so a *second* investigation turn is
        # legal again, then confirm it doesn't touch the first approval's
        # own already-recorded history entry.
        client.post(f"/api/v1/findings/{finding_id}/reverify")
        fake.script("v2_tool_turn", ScriptedToolCalls([("get_element_properties", {"entity_type": "IfcWindow"})]))
        fake.script("v2_tool_turn", ScriptedAnswer(["A completely different, later conclusion."]))
        second_investigation = _investigate(client, finding_id)
        assert second_investigation["status_code"] == 200

        final = client.get(f"/api/v1/findings/{finding_id}").json()

    assert final["pending_proposal"]["rationale"] == "A completely different, later conclusion."
    original_approval_entry = next(entry for entry in final["history"] if entry["proposal_snapshot"] is not None)
    assert original_approval_entry["proposal_snapshot"]["rationale"] == approved_rationale == "The IFC value 1.75 m is correct."
