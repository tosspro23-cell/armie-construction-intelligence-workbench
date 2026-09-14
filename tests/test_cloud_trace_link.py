"""D-028: a one-click Azure Portal link into this deployment's own
Application Insights telemetry for one specific request.

Two independently-generated trace-id schemes only become joinable because
main.py's chat() tags the current OpenTelemetry span with this app's own
trace_id -- covered here directly (not inferred from the URL alone) via a
recorder standing in for the real span, the same seam-injection discipline
this repo's provider-factory tests already use.

No Ollama, no network, no downloaded model, no real OpenTelemetry exporter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.graph import AgentService
from app.config import Settings, get_settings
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


# --- AgentService._cloud_trace_url, in isolation ----------------------------------

def test_cloud_trace_url_is_absent_when_tenant_and_resource_id_are_unconfigured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id="cloud-link-off", question="How many doors are there?", viewer_context=None)

    assert response.execution_metadata.get("cloud_trace_url") is None
    # The raw KQL fallback (D-029) is offered regardless of whether the URL
    # settings are configured -- it's plain text the user pastes into
    # whatever Application Insights resource they already have open.
    assert response.trace_id in response.execution_metadata.get("cloud_trace_query", "")


def test_cloud_trace_url_is_built_from_configured_tenant_and_resource_id_and_carries_the_trace_id(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        azure_tenant_id="11111111-2222-3333-4444-555555555555",
        app_insights_resource_id="/subscriptions/sub/resourceGroups/rg/providers/microsoft.insights/components/armiem3-insights",
    )
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id="cloud-link-on", question="How many doors are there?", viewer_context=None)

    url = response.execution_metadata.get("cloud_trace_url")
    assert url is not None
    assert url.startswith("https://portal.azure.com/#@11111111-2222-3333-4444-555555555555")
    assert "/resource/subscriptions/sub/resourceGroups/rg/providers/microsoft.insights/components/armiem3-insights/logs" in url
    assert response.trace_id in url  # the actual claim: this link names *this* request, not a generic dashboard


# --- main.py's chat() tags the real OpenTelemetry span ----------------------------

class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_chat_tags_the_current_otel_span_with_the_responses_own_trace_id_not_the_request_id(monkeypatch, tmp_path):
    """D-031: the original D-028 fix tagged the span *before* calling
    AgentService.invoke, using the caller-supplied request_id -- but
    invoke() always mints its own fresh trace_id internally
    (str(uuid4()), graph.py), completely independent of request_id.
    Everything else in the system (AuditStore, citations, cloud_trace_
    url/query) keys on response.trace_id, not request_id, so tagging with
    request_id pointed the whole cloud-provenance link at telemetry that
    was never actually associated with the answer. Found live, 2026-09-14,
    from a real Application Insights query returning zero rows for a real,
    successful answer.
    """
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()

    import app.main as main_module

    recorded_span = _RecordingSpan()
    monkeypatch.setattr(main_module.otel_trace, "get_current_span", lambda: recorded_span)

    with TestClient(main_module.app) as client:
        response = client.post("/api/v1/chat", json={"request_id": "trace-tag-1", "question": "How many doors are there?"})

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] != "trace-tag-1"  # AgentService.invoke's own fresh uuid4, not the request_id
    assert recorded_span.attributes.get("app.trace_id") == body["trace_id"]


def test_chat_tags_the_span_on_a_terminal_response_exit_using_that_responses_trace_id(monkeypatch, tmp_path):
    """_terminal_response's own early-exit paths (project-bind failure,
    timeout, cancelled, ...) never reach AgentService.invoke, and
    _terminal_response itself sets trace_id=request_id for the AgentResponse
    it builds -- so this is the one case where the two IDs are legitimately
    the same value. Exercised here via a genuinely-reachable exit
    (conversations.get raising on an unreadable thread, patched directly
    rather than trying to force a project-bind failure through real I/O),
    confirming _tag_span_with_trace_id's single call site handles a
    _terminal_response exit correctly without needing to know in advance
    which of the two IDs a given branch happens to use.
    """
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()

    import app.main as main_module

    recorded_span = _RecordingSpan()
    monkeypatch.setattr(main_module.otel_trace, "get_current_span", lambda: recorded_span)

    def _raise(*args, **kwargs):
        raise RuntimeError("conversation store unavailable")

    # Patched only after TestClient's own `with` entry runs `lifespan` and
    # constructs the real app.state.container -- patching the module-level
    # `app`'s container before that point patches a stale/nonexistent
    # object, since a fresh container replaces it on every lifespan start.
    with TestClient(main_module.app) as client:
        monkeypatch.setattr(main_module.app.state.container.conversation_store, "get", _raise)
        response = client.post("/api/v1/chat", json={"request_id": "trace-tag-2", "question": "How many doors are there?"})

    assert response.status_code == 200  # _terminal_response is itself an honest 200 (D-004/D-010), not an HTTP error
    body = response.json()
    assert body["disposition"] == "error"
    assert body["trace_id"] == "trace-tag-2"
    assert recorded_span.attributes.get("app.trace_id") == "trace-tag-2"
