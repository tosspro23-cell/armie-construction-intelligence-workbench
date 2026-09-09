"""Unit tests for require_api_key (SPEC-M5 §A/D).

Exercises the dependency function directly against app.config.get_settings
(an lru_cache'd singleton -- each test clears it via .cache_clear() and
monkeypatches the environment so tests don't leak state into each other or
depend on run order).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import get_settings
from app.security import require_api_key
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    monkeypatch.delenv("API_SHARED_SECRET", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unset_secret_allows_any_request_including_no_header():
    require_api_key(authorization=None)
    require_api_key(authorization="Bearer anything")


def test_set_secret_rejects_a_missing_header(monkeypatch):
    monkeypatch.setenv("API_SHARED_SECRET", "correct-horse-battery-staple")

    with pytest.raises(HTTPException) as excinfo:
        require_api_key(authorization=None)

    assert excinfo.value.status_code == 401


def test_set_secret_rejects_a_wrong_value(monkeypatch):
    monkeypatch.setenv("API_SHARED_SECRET", "correct-horse-battery-staple")

    with pytest.raises(HTTPException) as excinfo:
        require_api_key(authorization="Bearer wrong-guess")

    assert excinfo.value.status_code == 401


def test_set_secret_rejects_the_bare_secret_without_the_bearer_prefix(monkeypatch):
    monkeypatch.setenv("API_SHARED_SECRET", "correct-horse-battery-staple")

    with pytest.raises(HTTPException):
        require_api_key(authorization="correct-horse-battery-staple")


def test_set_secret_accepts_the_exact_matching_header(monkeypatch):
    monkeypatch.setenv("API_SHARED_SECRET", "correct-horse-battery-staple")

    require_api_key(authorization="Bearer correct-horse-battery-staple")


def test_a_real_http_request_without_the_header_is_rejected_end_to_end(monkeypatch, tmp_path):
    """The one behaviour a direct main.chat(...) call cannot exercise
    (SPEC-M5's verified-current-state note): FastAPI's dependency-injection
    pipeline only runs for a real routed HTTP request, not for calling a
    decorated route function directly as a plain coroutine -- every other
    test in this suite that drives app.main.chat()/resume() does the
    latter and would not catch a broken or missing auth dependency. This
    is the first test in this suite to route through app.main.app's real
    HTTP dispatch (TestClient), specifically because that distinction
    matters here.
    """
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILE", "armie_demo_schedule.pdf")
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("API_SHARED_SECRET", "correct-horse-battery-staple")
    get_settings.cache_clear()

    import app.main as main_module

    with TestClient(main_module.app) as client:
        unauthenticated = client.get("/api/v1/health")
        assert unauthenticated.status_code == 401

        authenticated = client.get(
            "/api/v1/health", headers={"Authorization": "Bearer correct-horse-battery-staple"}
        )
        assert authenticated.status_code == 200

    get_settings.cache_clear()
