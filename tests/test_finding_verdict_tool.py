"""D-064 item 2 (independent-review finding, 2026-09-19): a finding
investigation's conclusion used to be inferred from the model's own free-text
answer (`AgentService._extract_proposed_dimension`) -- a real, confirmed
fabrication risk (see `test_finding_proposal_extraction.py`), and a shape
that cannot express what a `missing_in_pdf`/`missing_in_ifc` investigation
actually concludes ("is this a real omission, or did you find it under a
different tag" is not a width or a height).

`submit_finding_verdict` is a tool offered only during a finding
investigation (`AgentService.invoke_v2`'s `include_verdict_tool`), forcing
the model to submit its conclusion as typed, code-validated arguments
instead. These tests cover two layers: `AgentService.build_finding_proposal`
directly (fast, exhaustive coverage of the verdict/value combinations,
including the safety net that still rejects an ungrounded structured value
exactly the way the old free-text extraction rejected an ungrounded
sentence), and one real end-to-end turn through the production dispatch
path (`_chat_v2` -> `invoke_v2` -> `_v2_dispatch_tool`) proving the tool
actually reaches the model, gets dispatched, and its arguments actually flow
into the persisted `AgentProposal` -- not just that the mapping function
alone behaves.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.graph import AgentService
from app.config import get_settings
from app.schemas.models import (
    AgentResponse,
    Citation,
    Disposition,
    EngineeringFinding,
    FindingStatus,
    VerificationStatus,
)
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


def _finding(**overrides) -> EngineeringFinding:
    defaults = dict(
        finding_id="f1", project_id="demo", source_set_id="demo-v2", trace_id="t1", tag="W02",
        finding_type="dimension_mismatch", severity="medium", status=FindingStatus.ACTION_REQUIRED,
        detail="W02: IFC 1.2x1.75 m vs PDF 1.2x1.7 m differ beyond the +/-0.01 m tolerance.",
        ifc_width_m=1.2, ifc_height_m=1.75, pdf_width_m=1.2, pdf_height_m=1.7, evidence_refs=[],
    )
    defaults.update(overrides)
    return EngineeringFinding(**defaults)


def _response(execution_metadata: dict) -> AgentResponse:
    return AgentResponse(
        thread_id="t1", trace_id="t1", disposition=Disposition.ANSWERED,
        answer_markdown="See the structured verdict for my conclusion.",
        citations=[Citation(evidence_id="e1", source_type="ifc", label="IFC IfcWindow Tag=W02.", locator={}, project_id="demo", source_set_id="demo-v2", source_file="armie_demo.ifc")],
        verification=VerificationStatus(status="verified", reason="test"),
        execution_metadata=execution_metadata,
    )


# --- build_finding_proposal: direct, exhaustive coverage -----------------------------

def test_dimension_confirmed_verdict_populates_the_matching_field():
    finding = _finding()
    response = _response({"finding_verdict": {"verdict": "dimension_confirmed", "confirmed_height_m": 1.75, "basis": "IFC properties confirm 1.75 m."}})

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict == "dimension_confirmed"
    assert proposal.verdict_basis == "IFC properties confirm 1.75 m."
    assert proposal.proposed_height_m == 1.75
    assert proposal.proposed_width_m is None  # never asserted a width verdict


def test_dimension_confirmed_verdict_with_an_ungrounded_value_is_not_adopted():
    """The safety net: a structured submission is still not, on its own,
    sufficient grounds to adopt a value -- it must match one of the
    finding's own known IFC/PDF candidates, the same discipline the old
    free-text extraction already enforced. `verdict`/`verdict_basis`
    (the model's categorical judgment) are kept regardless -- only the
    specific numeric claim is rejected.
    """
    finding = _finding()
    response = _response({"finding_verdict": {"verdict": "dimension_confirmed", "confirmed_height_m": 4.0, "basis": "Based on my analysis, 4.0 is correct."}})

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict == "dimension_confirmed"
    assert proposal.verdict_basis == "Based on my analysis, 4.0 is correct."
    assert proposal.proposed_height_m is None
    assert proposal.proposed_width_m is None


def test_genuine_omission_verdict_carries_no_dimension_claim():
    finding = _finding(tag="D04", finding_type="missing_in_pdf", ifc_width_m=0.9, ifc_height_m=2.1, pdf_width_m=None, pdf_height_m=None)
    response = _response({"finding_verdict": {"verdict": "genuine_omission", "basis": "No door/window schedule row named D04 anywhere on the supplied PDF pages."}})

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict == "genuine_omission"
    assert proposal.proposed_width_m is None
    assert proposal.proposed_height_m is None


def test_found_under_different_reference_verdict_is_recorded():
    finding = _finding(tag="W05", finding_type="missing_in_ifc", ifc_width_m=None, ifc_height_m=None, pdf_width_m=1.2, pdf_height_m=1.5)
    response = _response({"finding_verdict": {"verdict": "found_under_different_reference", "basis": "Matches IFC tag W01, confirmed not already matched to any other PDF row."}})

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict == "found_under_different_reference"
    assert "W01" in proposal.verdict_basis


def test_inconclusive_verdict_is_recorded_with_no_dimension_claim():
    finding = _finding()
    response = _response({"finding_verdict": {"verdict": "inconclusive", "basis": "Could not locate a schedule row after three differently-worded attempts."}})

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict == "inconclusive"
    assert proposal.proposed_width_m is None
    assert proposal.proposed_height_m is None


def test_no_verdict_tool_call_falls_back_to_free_text_extraction():
    """A turn that never reached submit_finding_verdict at all (e.g. hit
    the iteration cap first) must not silently produce an empty proposal
    -- the narrow free-text fallback still applies.
    """
    finding = _finding()
    response = AgentResponse(
        thread_id="t1", trace_id="t1", disposition=Disposition.ANSWERED,
        answer_markdown="The IFC height of 1.75m is confirmed correct based on the model's own recorded properties.",
        citations=[], verification=VerificationStatus(status="verified", reason="test"),
        execution_metadata={},
    )

    proposal = AgentService.build_finding_proposal(finding, response)

    assert proposal.verdict is None
    assert proposal.proposed_height_m == 1.75


# --- end-to-end: the tool is really offered, dispatched, and persisted ---------------

def _walk_to_action_required(client, finding_id: str) -> None:
    for action in ("acknowledge", "start_action"):
        response = client.post(f"/api/v1/findings/{finding_id}/transition", json={"action": action})
        assert response.status_code == 200, response.text


def _investigate(client, finding_id: str, thread_id: str = "verdict-thread") -> dict:
    response = client.post("/api/v1/chat", json={
        "question": "[finding investigation]", "engine": "v2", "thread_id": thread_id, "finding_id": finding_id,
    })
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.strip().split("\n\n") if line.startswith("data: ")]
    return {"status_code": response.status_code, "events": events}


def test_submit_finding_verdict_tool_reaches_the_model_and_its_arguments_are_persisted(monkeypatch, tmp_path) -> None:
    """End-to-end proof through the real production dispatch path: the
    model's own (scripted, but arbitrary-tool-name-capable) tool call is
    dispatched by `_v2_dispatch_tool`, carried through `invoke_v2`'s own
    `execution_metadata`, and `build_finding_proposal` reads it back off
    the real, completed `AgentResponse` -- not asserted against the
    mapping function in isolation.
    """
    _configure_env(monkeypatch, tmp_path)
    import app.main as main_module
    from app.agent.graph import AgentService as _AgentService
    from app.services import ServiceContainer
    from fakes.fake_provider import FakeModelProvider

    with TestClient(main_module.app) as client:
        fake = FakeModelProvider()
        fake.script("v2_tool_turn", ScriptedToolCalls([
            ("get_element_properties", {"entity_type": "IfcWindow"}),
            ("submit_finding_verdict", {"verdict": "dimension_confirmed", "confirmed_height_m": 1.75, "basis": "IFC properties confirm 1.75 m; PDF's 1.70 m could not be corroborated."}),
        ]))
        fake.script("v2_tool_turn", ScriptedAnswer(["The IFC value of 1.75 m is correct; see my submitted verdict."]))
        container = ServiceContainer(main_module.app.state.container.settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
        main_module.app.state.container = container
        main_module.app.state.agent = _AgentService(container)

        client.post("/api/v1/chat", json={"question": RECONCILIATION_QUESTION})
        finding_id = next(item["finding_id"] for item in client.get("/api/v1/findings").json() if item["tag"] == "W02")
        _walk_to_action_required(client, finding_id)

        result = _investigate(client, finding_id)
        final = client.get(f"/api/v1/findings/{finding_id}").json()

    assert result["status_code"] == 200
    tool_names_called = {event["tool_name"] for event in result["events"] if event["type"] == "tool_status"}
    assert "submit_finding_verdict" in tool_names_called
    proposal = final["pending_proposal"]
    assert proposal is not None
    assert proposal["verdict"] == "dimension_confirmed"
    assert proposal["proposed_height_m"] == 1.75
    assert "1.75" in proposal["verdict_basis"]
