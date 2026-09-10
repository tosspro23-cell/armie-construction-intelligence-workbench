"""Unit + end-to-end tests for D-016's request-ownership check.

Mirrors tests/test_security.py's structure: unit tests exercise
check_request_ownership/generate_session_id directly, and one real HTTP
test (TestClient) proves app.main.cancel_request/request_status actually
enforce it via FastAPI's real dispatch -- calling those functions directly
as plain coroutines would bypass Header() resolution the same way it
bypasses Depends() (see test_security.py's own end-to-end test docstring
for why that distinction matters here).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.security import check_request_ownership, generate_session_id
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    monkeypatch.delenv("API_SHARED_SECRET", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_generate_session_id_produces_distinct_unguessable_values():
    first, second = generate_session_id(), generate_session_id()
    assert first != second
    assert len(first) >= 32


def test_ownership_allows_the_owning_session():
    check_request_ownership({"session_id": "session-a"}, "session-a")


def test_ownership_rejects_a_different_session():
    with pytest.raises(HTTPException) as excinfo:
        check_request_ownership({"session_id": "session-a"}, "session-b")
    assert excinfo.value.status_code == 404


def test_ownership_rejects_a_missing_caller_session_when_the_record_has_an_owner():
    with pytest.raises(HTTPException) as excinfo:
        check_request_ownership({"session_id": "session-a"}, None)
    assert excinfo.value.status_code == 404


def test_ownership_is_unrestricted_for_a_record_with_no_owner():
    """Pre-D-016 compatibility (documented, not an oversight): a record
    created without X-Session-Id (a direct API caller that never adopted
    the session flow) stays open to any caller, exactly as before this fix.
    """
    check_request_ownership({"session_id": None}, None)
    check_request_ownership({"session_id": None}, "anyone")


def test_a_real_http_caller_cannot_query_or_cancel_another_sessions_request(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()

    import app.main as main_module

    with TestClient(main_module.app) as client:
        # /api/v1/session itself needs no session header -- it's what
        # issues one -- but does sit behind require_api_key like every
        # other route (unset API_SHARED_SECRET here, so it's a no-op).
        session_a = client.post("/api/v1/session").json()["session_id"]
        session_b = client.post("/api/v1/session").json()["session_id"]
        assert session_a != session_b

        # Fabricate a request record directly rather than running the full
        # agent pipeline through a real chat call -- request-ownership is
        # independent of what the request actually did, and this keeps the
        # test from needing a live/fake model wired through TestClient.
        main_module.app.state.requests["req-1"] = {
            "status": "running", "stage": "queued", "task": None,
            "trace_id": "req-1", "session_id": session_a,
        }

        # The owner can see and cancel it.
        owner_status = client.get("/api/v1/requests/req-1", headers={"X-Session-Id": session_a})
        assert owner_status.status_code == 200

        # A different session cannot -- 404, not 403, so existence isn't
        # confirmed to a non-owner either.
        other_status = client.get("/api/v1/requests/req-1", headers={"X-Session-Id": session_b})
        assert other_status.status_code == 404

        no_session_status = client.get("/api/v1/requests/req-1")
        assert no_session_status.status_code == 404

        other_cancel = client.post("/api/v1/requests/req-1/cancel", headers={"X-Session-Id": session_b})
        assert other_cancel.status_code == 404

        owner_cancel = client.post("/api/v1/requests/req-1/cancel", headers={"X-Session-Id": session_a})
        assert owner_cancel.status_code == 200

        # A record with no session_id (direct API caller, no X-Session-Id
        # sent at chat time) stays unrestricted -- pre-D-016 behaviour.
        main_module.app.state.requests["req-2"] = {
            "status": "running", "stage": "queued", "task": None,
            "trace_id": "req-2", "session_id": None,
        }
        unowned_status = client.get("/api/v1/requests/req-2", headers={"X-Session-Id": session_b})
        assert unowned_status.status_code == 200

    get_settings.cache_clear()
