"""SPEC-M7: the Azure AI Search retrieval seam, CI-safe (fake search
client + fake embedding provider injected via ServiceContainer's factory
seam, D-007 discipline -- production `app.retrieval`/`app.providers.
factory` globals are never monkeypatched as a substitute). Every test
here drives the real, production `AgentService.invoke()` path, not an
internal method called directly -- this project's own established
testing convention (see tests/test_pdf_deterministic_extraction.py and
friends): a fake/stub is injected at the factory seam, never a private
method invoked out of context.

The real-index evidence (does hybrid retrieval actually rank
rfi_log_047.pdf correctly for the Panel-E question, and what
azure_search_relevance_threshold was set to and why) is
docs/reports/2026-09-10-m7-azure-ai-search-baseline.md, not here -- that
claim can only be tested against a live Azure AI Search index, per the
spec's own Acceptance criteria, and is not a CI gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agent.graph import AgentService
from app.config import Settings
from app.services import ProjectResources, ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
CORPUS = "corpus"


def _demo(container: ServiceContainer) -> ProjectResources:
    return asyncio.run(container.get_project("demo"))


class FakeSearchClient:
    def __init__(self, results=None, raise_error=None):
        self.results = results or []
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def search(self, *, search_text, vector_queries, select, top):
        self.calls.append({"search_text": search_text, "top": top})
        if self.raise_error:
            raise self.raise_error
        return iter(self.results)


class FakeEmbeddingProvider:
    name = "fake"
    model = "fake-embed"

    def __init__(self, vector=None):
        self.vector = vector or [0.1, 0.2, 0.3]
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return self.vector


def _settings(tmp_path: Path, pdf_files: list[str]) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=pdf_files,
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    return settings


def _service(settings: Settings, search_client, embedding_provider) -> tuple[ServiceContainer, AgentService]:
    fake_model = FakeModelProvider()
    container = ServiceContainer(
        settings,
        text_provider_factory=lambda s: fake_model, vision_provider_factory=lambda s: fake_model,
        search_client_factory=lambda s: search_client, embedding_provider_factory=lambda s: embedding_provider,
    )
    return container, AgentService(container)


# --- opt-in-when-unconfigured (mirrors database_url/api_shared_secret) -----------

def test_retrieval_is_skipped_when_search_client_is_not_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=None, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="no-search-client", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert response.answer_markdown == "I could not find this field with a confident match in any configured document."
    assert embedding_provider.calls == 0  # never even asked to embed -- no wasted cost


def test_retrieval_is_skipped_when_embedding_provider_is_not_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[{"filename": "schedule_l2_east.pdf", "@search.score": 0.9}])
    container, service = _service(settings, search_client=search_client, embedding_provider=None)

    response = service.invoke(project_resources=_demo(container), thread_id="no-embedding-provider", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert response.answer_markdown == "I could not find this field with a confident match in any configured document."
    assert search_client.calls == []  # never even queried


# --- recall failure becomes a directed miss, not a blanket one -------------------

def test_a_recall_failure_becomes_a_directed_miss_when_retrieval_finds_a_candidate_above_threshold(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[
        {"filename": "schedule_l2_east.pdf", "@search.score": 0.9},  # above the real 0.025 threshold
    ])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="directed-miss", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert "schedule_l2_east.pdf" in response.answer_markdown
    assert "most relevant" in response.answer_markdown
    assert embedding_provider.calls == 1
    assert search_client.calls == [{"search_text": "What is the connected load for Panel-E?", "top": 5}]
    assert response.execution_metadata.get("model_call_count", 0) == 1  # the embedding call, disclosed


def test_a_directed_miss_emits_a_dedicated_retrieval_evaluated_audit_event(tmp_path: Path) -> None:
    """SPEC-M7 addendum (D-018): the retrieval evidence that actually
    drove this decision must be a distinct, easy-to-find audit event --
    not only a key buried inside clarification_requested's payload among
    many other Raw Trace entries. apps/web/src/main.tsx looks for this
    exact event_type to render a dedicated "AI Search Retrieval" card.
    """
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[
        {"filename": "schedule_l2_east.pdf", "@search.score": 0.9},
        {"filename": "schedule_l2_west.pdf", "@search.score": 0.001},
    ])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)
    container = service.container

    response = service.invoke(project_resources=_demo(container), thread_id="dedicated-event", viewer_context=None, question="What is the connected load for Panel-E?")

    events = [event for event in container.audit_store.by_trace(response.trace_id) if event.event_type == "retrieval_evaluated"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["documents_evaluated"] == [
        {"filename": "schedule_l2_east.pdf", "score": 0.9},
        {"filename": "schedule_l2_west.pdf", "score": 0.001},
    ]
    assert payload["relevance_threshold"] == settings.azure_search_relevance_threshold
    assert payload["directed_candidates"] == ["schedule_l2_east.pdf"]  # only the one that cleared the bar


def test_no_dedicated_retrieval_event_when_retrieval_is_not_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    container, service = _service(settings, search_client=None, embedding_provider=None)
    container = service.container

    response = service.invoke(project_resources=_demo(container), thread_id="no-event", viewer_context=None, question="What is the connected load for Panel-E?")

    events = [event for event in container.audit_store.by_trace(response.trace_id) if event.event_type == "retrieval_evaluated"]
    assert events == []


def test_a_recall_failure_stays_a_blanket_miss_when_nothing_clears_the_threshold(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[
        {"filename": "schedule_l2_east.pdf", "@search.score": 0.001},  # well below threshold
    ])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="blanket-miss", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert response.answer_markdown == "I could not find this field with a confident match in any configured document."
    assert response.execution_metadata.get("model_call_count", 0) == 1  # retrieval still ran and still cost a call
    # Still worth surfacing in a dedicated card: retrieval ran, found a
    # candidate, but it didn't clear the bar -- that's useful evidence too.
    events = [event for event in service.container.audit_store.by_trace(response.trace_id) if event.event_type == "retrieval_evaluated"]
    assert len(events) == 1
    assert events[0].payload["directed_candidates"] == []


def test_a_failed_search_call_degrades_to_the_blanket_miss_not_an_unhandled_error(tmp_path: Path) -> None:
    """Retrieval is advisory (SPEC-M7's own framing): a transient Azure AI
    Search failure must not turn an already-honest clarification_required
    into a 500 -- it degrades to the naive baseline's existing message.
    """
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(raise_error=RuntimeError("service unavailable"))
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="search-failure", viewer_context=None, question="What is the connected load for Panel-E?")

    assert response.disposition.value == "clarification_required"
    assert response.answer_markdown == "I could not find this field with a confident match in any configured document."


# --- precision-failure (collision) and genuine single-match are unaffected -------

def test_retrieval_is_never_invoked_when_native_lookup_already_found_multiple_hits(tmp_path: Path) -> None:
    """The precision-failure (collision) case must not regress: retrieval
    only runs in the zero-hit branch (SPEC-M7 §C's explicit non-goal --
    it does not tie-break a genuine collision by relevance score).
    Verified by a call-count assertion, not just the final disposition,
    per this project's own precedent (F12) for proving a code path was
    never reached, not merely that its absence didn't change the answer.
    """
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[{"filename": "schedule_l2_east.pdf", "@search.score": 0.9}])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="collision-unaffected", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "clarification_required"
    assert "schedule_l2_east.pdf" in response.answer_markdown and "schedule_l2_west.pdf" in response.answer_markdown
    assert search_client.calls == []
    assert embedding_provider.calls == 0
    assert response.execution_metadata.get("model_call_count", 0) == 0


def test_retrieval_is_never_invoked_when_native_lookup_already_answered(tmp_path: Path) -> None:
    settings = _settings(tmp_path, [f"{CORPUS}/schedule_l2_east.pdf", f"{CORPUS}/schedule_l2_west.pdf"])
    search_client = FakeSearchClient(results=[{"filename": "schedule_l2_east.pdf", "@search.score": 0.9}])
    embedding_provider = FakeEmbeddingProvider()
    container, service = _service(settings, search_client=search_client, embedding_provider=embedding_provider)

    response = service.invoke(project_resources=_demo(container), thread_id="unique-match-unaffected", viewer_context=None, question="What is the connected load for Panel-C?")

    assert response.disposition.value == "answered"
    assert search_client.calls == []
    assert embedding_provider.calls == 0
