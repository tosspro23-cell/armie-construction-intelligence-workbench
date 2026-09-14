"""SPEC-M11: Engineering Finding Workflow.

Covers the state machine (app/finding_workflow.py) table-driven, upsert-
not-duplicate behaviour across two reconciliation runs, and re-verify's
two real branches -- proven with a genuine fresh re-read of the sources,
not by trusting a finding's own stored values (see
AgentService.reverify_reconciliation_tag).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.finding_workflow import (
    IllegalFindingTransition,
    resolve_reverify_outcome,
    validate_transition,
)
from app.schemas.models import FindingStatus
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
