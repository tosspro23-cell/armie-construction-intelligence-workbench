"""SPEC-M9: multi-project workspace via Azure Data Lake Storage Gen2.

Covers ServiceContainer.get_project's opt-in/caching/concurrency/failure
contract (§C) and ConversationStore.bind_project's claim-once/reject-
mismatch semantics (§D), using a fake Data Lake client (D-007 discipline,
mirroring tests/test_evidence_blob_persistence.py's/test_azure_ai_search_
retrieval.py's own fake-client pattern) -- no live Azure, no network.

The real-ADLS evidence (does an actual project switch answer correctly
against the live app, does the directory-ACL identity's three-part
verification pass) is a separate deployment-baseline report, per this
project's established split between CI-safe fake-driven tests and a real-
service report -- see docs/reports/*-m9-*.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path

from app.config import Settings
from app.persistence.conversation_store import InMemoryConversationStore
from app.services import ProjectNotFoundError, ServiceContainer
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
WESTGATE_IFC = ROOT / "demo_data" / "projects" / "westgate" / "westgate.ifc"
WESTGATE_PDF = ROOT / "demo_data" / "projects" / "westgate" / "westgate_schedule.pdf"


class FakeDataLakeClient:
    """Matches app.adls.DataLakeClient's own interface (read_file(path) ->
    bytes) exactly, so ServiceContainer's download logic is exercised
    without needing the real azure-storage-file-datalake SDK at all.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.read_calls: list[str] = []
        self.read_lock = threading.Lock()

    def read_file(self, path: str) -> bytes:
        with self.read_lock:
            self.read_calls.append(path)
        return self.files[path]


class CorruptingDataLakeClient:
    """Returns a byte-flipped copy of whatever the real file's bytes would
    be, for the download-integrity-verification tests below.
    """

    def __init__(self, real_files: dict[str, bytes], corrupt_path: str) -> None:
        self._real_files = real_files
        self._corrupt_path = corrupt_path

    def read_file(self, path: str) -> bytes:
        data = self._real_files[path]
        return data if path != self._corrupt_path else b"corrupted-" + data


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
        project_cache_dir=tmp_path / "project_cache",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_runtime_directories()
    return settings


def _westgate_files() -> dict[str, bytes]:
    return {
        "westgate/ifc/westgate.ifc": WESTGATE_IFC.read_bytes(),
        "westgate/pdf/westgate_schedule.pdf": WESTGATE_PDF.read_bytes(),
    }


def _demo_files() -> dict[str, bytes]:
    # SPEC-M9 §B: once ADLS mode is on, "demo" is just another project_id
    # in the registry, backed by ADLS like westgate is -- it does NOT stay
    # on the local-filesystem path just because it is the pre-M9 default.
    return {
        "demo/ifc/armie_demo.ifc": (ROOT / "demo_data" / "armie_demo.ifc").read_bytes(),
        "demo/pdf/armie_demo_schedule.pdf": (ROOT / "demo_data" / "armie_demo_schedule.pdf").read_bytes(),
    }


# --- opt-in-when-unconfigured, no silent fallback to "demo" ------------------------

def test_get_project_demo_resolves_locally_when_adls_is_unset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = ServiceContainer(settings)

    resources = asyncio.run(container.get_project("demo"))

    # The exact, eagerly-built fields every deployment before this
    # milestone already has -- get_project only names them, not a new
    # code path, when ADLS is off.
    assert resources.ifc_repository is container.ifc_repository
    assert resources.document_analyzers is container.document_analyzers
    assert resources.manifest.project_id == "demo"


def test_get_project_rejects_a_non_demo_project_when_adls_is_unset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = ServiceContainer(settings)

    try:
        asyncio.run(container.get_project("westgate"))
        raise AssertionError("expected ProjectNotFoundError")
    except ProjectNotFoundError:
        pass


def test_get_project_rejects_an_unknown_project_id_even_with_adls_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    container = ServiceContainer(settings, datalake_client_factory=lambda s: FakeDataLakeClient({}))

    try:
        asyncio.run(container.get_project("not-a-real-project"))
        raise AssertionError("expected ProjectNotFoundError")
    except ProjectNotFoundError:
        pass


# --- download, verify, cache (§C) --------------------------------------------------

def test_get_project_downloads_and_verifies_westgate_when_adls_is_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient(_westgate_files())
    container = ServiceContainer(settings, datalake_client_factory=lambda s: fake_client)

    resources = asyncio.run(container.get_project("westgate"))

    assert resources.ifc_repository.path.exists()
    assert resources.ifc_repository.path.read_bytes() == WESTGATE_IFC.read_bytes()
    assert resources.document_analyzer.pdf_path.read_bytes() == WESTGATE_PDF.read_bytes()
    assert sorted(fake_client.read_calls) == sorted(["westgate/ifc/westgate.ifc", "westgate/pdf/westgate_schedule.pdf"])


def test_get_project_caches_across_calls_with_no_further_downloads(tmp_path: Path) -> None:
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient(_westgate_files())
    container = ServiceContainer(settings, datalake_client_factory=lambda s: fake_client)

    first = asyncio.run(container.get_project("westgate"))
    calls_after_first = len(fake_client.read_calls)
    second = asyncio.run(container.get_project("westgate"))

    assert first is second
    assert len(fake_client.read_calls) == calls_after_first  # no new downloads


def test_concurrent_first_access_triggers_exactly_one_download(tmp_path: Path) -> None:
    """The gap an independent review of this spec's first draft correctly
    flagged: "one replica" bounds cross-*process* races, not cross-
    *request* ones -- this process already serves concurrent requests.
    """
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient(_westgate_files())
    container = ServiceContainer(settings, datalake_client_factory=lambda s: fake_client)

    async def run_concurrent():
        return await asyncio.gather(container.get_project("westgate"), container.get_project("westgate"))

    first, second = asyncio.run(run_concurrent())

    assert first is second
    # Exactly one download of the two files (ifc + pdf) -- not two
    # downloads from two independent, racing first-accesses.
    assert len(fake_client.read_calls) == 2


def test_a_corrupted_download_is_never_cached_and_a_retry_succeeds_cleanly(tmp_path: Path) -> None:
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    real_files = _westgate_files()
    corrupting_client = CorruptingDataLakeClient(real_files, "westgate/pdf/westgate_schedule.pdf")
    container = ServiceContainer(settings, datalake_client_factory=lambda s: corrupting_client)

    try:
        asyncio.run(container.get_project("westgate"))
        raise AssertionError("expected a content_sha256 mismatch to raise")
    except ValueError as error:
        assert "content_sha256" in str(error)

    cache_dir = settings.project_cache_dir / "westgate" / "westgate-v1"
    assert not cache_dir.exists()
    # No leftover temp directory either -- the failed download's staging
    # directory must be cleaned up, not left behind as disk debris.
    leftover = list(settings.project_cache_dir.glob("tmp*"))
    assert leftover == []

    # A retry with a good client (the next request, in production) must
    # succeed cleanly -- the earlier failure must not have left the cache
    # directory itself in some unusable half-created state.
    good_client = FakeDataLakeClient(real_files)
    container.datalake_client_factory = lambda s: good_client
    resources = asyncio.run(container.get_project("westgate"))
    assert resources.ifc_repository.path.read_bytes() == WESTGATE_IFC.read_bytes()


def test_cache_key_includes_source_set_id_not_project_id_alone(tmp_path: Path) -> None:
    """SPEC-M9 §F: a future source_set_id bump must not silently serve
    stale cached content under the same key.
    """
    settings = _settings(tmp_path, adls_account_url="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient(_westgate_files())
    container = ServiceContainer(settings, datalake_client_factory=lambda s: fake_client)
    asyncio.run(container.get_project("westgate"))

    manifest = container._project_registry["westgate"]
    assert ("westgate", manifest.source_set_id) in container._project_resources
    assert (settings.project_cache_dir / "westgate" / manifest.source_set_id).exists()


# --- ConversationStore.bind_project: claim-once, reject-mismatch (§D) --------------

def test_bind_project_claims_an_unbound_thread() -> None:
    store = InMemoryConversationStore()
    assert store.bind_project("thread-1", "westgate") == "westgate"


def test_bind_project_returns_the_existing_binding_unchanged_on_a_second_call() -> None:
    store = InMemoryConversationStore()
    store.bind_project("thread-1", "demo")
    # A second, differently-projected call must never silently overwrite
    # the first binding.
    assert store.bind_project("thread-1", "westgate") == "demo"


def test_bind_project_is_race_safe_under_concurrent_first_bind() -> None:
    """Store calls run via asyncio.to_thread in production, i.e. real OS
    threads racing against each other, not just concurrent coroutines on
    one event loop -- this reproduces that with a real ThreadPoolExecutor.
    """
    store = InMemoryConversationStore()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(store.bind_project, "thread-race", "demo")
        future_b = pool.submit(store.bind_project, "thread-race", "westgate")
        result_a, result_b = future_a.result(), future_b.result()

    # Whichever one actually won the race, both callers must agree on the
    # same, single outcome -- never each seeing a different project.
    assert result_a == result_b
    assert result_a in {"demo", "westgate"}


# --- End-to-end via the real HTTP dispatch path (TestClient) -----------------------
#
# lifespan (app/main.py) constructs app.state.container from get_settings()
# itself, so a container built and assigned *before* `with TestClient(app)`
# is simply discarded once the ASGI lifespan runs -- confirmed live: an
# earlier version of these tests built the container up front and every
# assertion happened to still pass, for the wrong reason (the fast
# deterministic path never touched the discarded fake at all). The
# established pattern (tests/test_evidence_blob_persistence.py) instead
# points get_settings() at the right env via monkeypatch, lets lifespan
# build the container normally, then reassigns specific *factory*
# attributes afterward -- ServiceContainer stores each factory as a plain,
# reassignable attribute for exactly this.

def _configure_env(monkeypatch, tmp_path: Path, **env_overrides: str) -> None:
    from app.config import get_settings

    env = {
        "DATA_DIR": str(ROOT / "demo_data"), "IFC_FILE": "armie_demo.ifc",
        "PDF_FILES": '["armie_demo_schedule.pdf"]',
        "AUDIT_STORE_PATH": str(tmp_path / "audit.jsonl"), "EVIDENCE_DIR": str(tmp_path / "evidence"),
        "PROJECT_CACHE_DIR": str(tmp_path / "project_cache"),
    }
    env.update(env_overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_demo_is_unaffected_by_adls_mode_being_available_for_other_projects(monkeypatch, tmp_path: Path) -> None:
    """The direct regression proof for SPEC-M9's own core invariant: every
    existing single-project deployment must be provably unaffected. Same
    question, same answer, whether ADLS is configured (for westgate, and
    for "demo" itself once ADLS mode is on -- SPEC-M9 §B: "demo" becomes
    just another registry entry, backed by ADLS like westgate is, not a
    permanently-local-only special case) or off entirely.
    """
    import app.main as main_module
    from app.config import get_settings

    _configure_env(monkeypatch, tmp_path / "local")
    with TestClient(main_module.app) as client:
        response_local = client.post("/api/v1/chat", json={"question": "What is the connected load for Panel-A?"})
    get_settings.cache_clear()

    _configure_env(monkeypatch, tmp_path / "adls", ADLS_ACCOUNT_URL="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient({**_demo_files(), **_westgate_files()})
    with TestClient(main_module.app) as client:
        main_module.app.state.container.datalake_client_factory = lambda s: fake_client
        response_adls = client.post("/api/v1/chat", json={"question": "What is the connected load for Panel-A?"})
    get_settings.cache_clear()

    assert response_local.status_code == response_adls.status_code == 200
    assert response_local.json()["answer_markdown"] == response_adls.json()["answer_markdown"] == "44.50"
    # Only "demo"'s two files were ever requested -- "westgate" was never
    # touched by a request that never named it.
    assert sorted(fake_client.read_calls) == sorted(["demo/ifc/armie_demo.ifc", "demo/pdf/armie_demo_schedule.pdf"])


def test_continuing_a_bound_thread_against_a_different_project_is_rejected_with_409(monkeypatch, tmp_path: Path) -> None:
    import app.main as main_module
    from app.config import get_settings

    _configure_env(monkeypatch, tmp_path, ADLS_ACCOUNT_URL="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient({**_demo_files(), **_westgate_files()})
    with TestClient(main_module.app) as client:
        main_module.app.state.container.datalake_client_factory = lambda s: fake_client

        first = client.post("/api/v1/chat", json={"project_id": "demo", "question": "What is the connected load for Panel-A?"})
        assert first.status_code == 200
        thread_id = first.json()["thread_id"]

        mismatched = client.post("/api/v1/chat", json={"thread_id": thread_id, "project_id": "westgate", "question": "test"})
        assert mismatched.status_code == 409
    get_settings.cache_clear()


def test_resume_stays_on_the_bound_project_not_demo(monkeypatch, tmp_path: Path) -> None:
    """The direct regression test for the resume() gap found live by
    independent review (main.py previously reconstructed ChatRequest with
    no project information at all, silently defaulting every resume to
    "demo"). Drives a real clarification on "westgate" via the deployed
    dispatch path, then resumes it -- the resumed answer must still be
    westgate's own PDF, never demo's. The resume answer is a complete,
    self-contained question (resume() forwards it verbatim as the next
    turn's `question`) rather than a bare board name, so this exercises
    the same zero-model-call deterministic path already proven above,
    without depending on the router's own multi-turn plan-continuation
    heuristics -- a different, unrelated behaviour this test does not
    intend to characterize.
    """
    import app.main as main_module
    from app.config import get_settings

    _configure_env(monkeypatch, tmp_path, ADLS_ACCOUNT_URL="https://fake.dfs.core.windows.net")
    fake_client = FakeDataLakeClient({**_demo_files(), **_westgate_files()})
    with TestClient(main_module.app) as client:
        main_module.app.state.container.datalake_client_factory = lambda s: fake_client

        # A structural miss on westgate (no record named at all) ->
        # clarification_required, resumable, zero model calls.
        clarify = client.post("/api/v1/chat", json={"project_id": "westgate", "question": "What is the connected load?"})
        assert clarify.status_code == 200
        assert clarify.json()["disposition"] == "clarification_required"
        thread_id = clarify.json()["thread_id"]

        resumed = client.post(f"/api/v1/chat/{thread_id}/resume", json={"answer": "What is the connected load for Panel-A?"})
        assert resumed.status_code == 200
    get_settings.cache_clear()

    assert "51.20" in resumed.json()["answer_markdown"]  # westgate's own value, not demo's 44.50
