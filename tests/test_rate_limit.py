"""D-019: per-caller rate limiting on /api/v1/chat and /api/v1/chat/{id}/resume.

Closes the second half of D-012 Finding 1 that D-016 (per-caller request
ownership) explicitly left open: "rate limiting was also not bundled in."

Two levels, mirroring tests/test_security.py's own structure: unit tests
on InMemorySlidingWindowRateLimiter directly (no FastAPI, no network, a
fake clock so nothing sleeps), and one TestClient-based end-to-end test
proving the real HTTP path enforces it -- Header()-resolved dependencies
don't run when a route function is called directly as a plain coroutine,
the same distinction test_security.py's own end-to-end test exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.rate_limit import InMemorySlidingWindowRateLimiter
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_REQUESTS_PER_MINUTE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- InMemorySlidingWindowRateLimiter in isolation --------------------------------

def test_allows_requests_up_to_the_limit_then_rejects():
    clock = iter([0.0, 0.0, 0.0]).__next__
    limiter = InMemorySlidingWindowRateLimiter(limit=2, window_seconds=60.0, clock=clock)

    assert limiter.allow("a") == (True, 0.0)
    assert limiter.allow("a") == (True, 0.0)
    allowed, retry_after = limiter.allow("a")
    assert allowed is False
    assert retry_after == pytest.approx(60.0)


def test_different_keys_have_independent_budgets():
    limiter = InMemorySlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=lambda: 0.0)

    assert limiter.allow("session-a") == (True, 0.0)
    assert limiter.allow("session-b") == (True, 0.0)
    assert limiter.allow("session-a")[0] is False
    assert limiter.allow("session-b")[0] is False


def test_a_request_is_allowed_again_once_the_window_slides_past_it():
    times = iter([0.0, 61.0])
    limiter = InMemorySlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=lambda: next(times))

    assert limiter.allow("a") == (True, 0.0)
    assert limiter.allow("a") == (True, 0.0)  # 61s later, the first hit already aged out


# --- real HTTP end-to-end -----------------------------------------------------------

def test_a_real_http_caller_is_rate_limited_after_the_configured_number_of_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
    get_settings.cache_clear()

    import app.main as main_module

    with TestClient(main_module.app) as client:
        body = {"question": "What is the connected load for Panel-A?"}

        first = client.post("/api/v1/chat", json=body, headers={"X-Session-Id": "tab-1"})
        assert first.status_code == 200

        second = client.post("/api/v1/chat", json=body, headers={"X-Session-Id": "tab-1"})
        assert second.status_code == 429
        assert "Retry-After" in second.headers

        # A different session gets its own, independent budget -- not
        # starved by another tab's usage.
        other_tab = client.post("/api/v1/chat", json=body, headers={"X-Session-Id": "tab-2"})
        assert other_tab.status_code == 200

    get_settings.cache_clear()


def test_rate_limiting_is_a_no_op_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()

    import app.main as main_module

    with TestClient(main_module.app) as client:
        body = {"question": "What is the connected load for Panel-A?"}
        for _ in range(5):
            response = client.post("/api/v1/chat", json=body, headers={"X-Session-Id": "tab-1"})
            assert response.status_code == 200

    get_settings.cache_clear()
