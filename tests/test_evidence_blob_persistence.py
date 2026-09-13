"""SPEC-M8 (D-020): evidence crop persistence via Azure Blob Storage.

Closes the one piece SPEC-M4/D-014 (OD-24) explicitly deferred: cited PDF
evidence crops still live only on local Container App disk, so a revision
replacement breaks old citations even though the database describing them
survives fine.

Fake blob container client injected via ServiceContainer's factory seam
(D-007 discipline) -- production app.evidence_storage's global is never
monkeypatched as a substitute. No live Azure, no network.
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


class _FakeBlobDownloader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class FakeBlobContainerClient:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.upload_calls: list[str] = []

    def upload_blob(self, name: str, data: bytes, overwrite: bool = True) -> None:
        self.upload_calls.append(name)
        self.blobs[name] = data

    def download_blob(self, name: str) -> _FakeBlobDownloader:
        if name not in self.blobs:
            raise KeyError(f"no such blob: {name}")
        return _FakeBlobDownloader(self.blobs[name])


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"],
        audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence",
    )
    settings.ensure_runtime_directories()
    return settings


# --- opt-in-when-unconfigured -------------------------------------------------------

def test_get_blob_container_client_returns_none_when_unset(monkeypatch):
    from app.evidence_storage import get_blob_container_client

    monkeypatch.delenv("EVIDENCE_STORAGE_ACCOUNT_URL", raising=False)
    get_settings.cache_clear()
    assert get_blob_container_client(get_settings()) is None
    get_settings.cache_clear()


# --- crop_evidence's dual-write ------------------------------------------------------

def test_crop_evidence_uploads_to_blob_storage_when_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake_client = FakeBlobContainerClient()
    fake_model = FakeModelProvider()
    container = ServiceContainer(
        settings, text_provider_factory=lambda s: fake_model, vision_provider_factory=lambda s: fake_model,
        blob_container_client_factory=lambda s: fake_client,
    )
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id="t", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "answered"
    crop_name = response.citations[0].locator["evidence_crop"]
    assert crop_name is not None
    assert crop_name in fake_client.upload_calls
    assert crop_name in fake_client.blobs
    assert fake_client.blobs[crop_name].startswith(b"\x89PNG")  # a real PNG, not a stub


def test_crop_evidence_still_writes_locally_when_blob_storage_is_configured(tmp_path: Path) -> None:
    """The local write is unchanged/additive (§B) -- not replaced -- so no
    call site or return type needs to change.
    """
    settings = _settings(tmp_path)
    fake_client = FakeBlobContainerClient()
    fake_model = FakeModelProvider()
    container = ServiceContainer(
        settings, text_provider_factory=lambda s: fake_model, vision_provider_factory=lambda s: fake_model,
        blob_container_client_factory=lambda s: fake_client,
    )
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id="t", viewer_context=None, question="What is the connected load for Panel-A?")

    crop_name = response.citations[0].locator["evidence_crop"]
    assert (settings.evidence_dir / crop_name).exists()


def test_a_failed_blob_upload_does_not_fail_the_request(tmp_path: Path) -> None:
    class RaisingBlobContainerClient:
        def upload_blob(self, name, data, overwrite=True):
            raise RuntimeError("storage account unreachable")

    settings = _settings(tmp_path)
    fake_model = FakeModelProvider()
    container = ServiceContainer(
        settings, text_provider_factory=lambda s: fake_model, vision_provider_factory=lambda s: fake_model,
        blob_container_client_factory=lambda s: RaisingBlobContainerClient(),
    )
    service = AgentService(container)

    response = service.invoke(project_resources=_demo(container), thread_id="t", viewer_context=None, question="What is the connected load for Panel-A?")

    assert response.disposition.value == "answered"
    crop_name = response.citations[0].locator["evidence_crop"]
    assert (settings.evidence_dir / crop_name).exists()  # local write still succeeded


# --- GET /api/v1/evidence/{filename}: blob-first, local-fallback (§C) --------------

def test_evidence_endpoint_serves_from_blob_storage_when_present(monkeypatch, tmp_path):
    """The actual claim this milestone exists to prove: an evidence crop
    stays retrievable via GET /api/v1/evidence/{filename} purely from blob
    storage, with no local file at all -- simulating exactly what a
    Container App revision replacement leaves behind (a fresh local disk,
    the old crop nowhere on it).
    """
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    get_settings.cache_clear()

    fake_client = FakeBlobContainerClient()
    fake_client.blobs["pdf-crop-1-deadbeef.png"] = b"\x89PNG-fake-bytes-from-blob-storage"

    import app.main as main_module

    with TestClient(main_module.app) as client:
        # ServiceContainer.blob_container_client_factory is a plain,
        # reassignable attribute for exactly this -- mirrors how other
        # tests substitute a fake ConversationStore/AuditStore post-
        # construction rather than monkeypatching a production global.
        main_module.app.state.container.blob_container_client_factory = lambda s: fake_client

        response = client.get("/api/v1/evidence/pdf-crop-1-deadbeef.png")

        assert response.status_code == 200
        assert response.content == b"\x89PNG-fake-bytes-from-blob-storage"

    get_settings.cache_clear()


def test_evidence_endpoint_falls_back_to_local_when_blob_storage_does_not_have_it(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setenv("EVIDENCE_DIR", str(evidence_dir))
    get_settings.cache_clear()

    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "pdf-crop-1-local-only.png").write_bytes(b"\x89PNG-local-only-bytes")
    fake_client = FakeBlobContainerClient()  # empty -- never had this blob

    import app.main as main_module

    with TestClient(main_module.app) as client:
        main_module.app.state.container.blob_container_client_factory = lambda s: fake_client

        response = client.get("/api/v1/evidence/pdf-crop-1-local-only.png")

        assert response.status_code == 200
        assert response.content == b"\x89PNG-local-only-bytes"

    get_settings.cache_clear()


def test_evidence_endpoint_rejects_the_literal_parent_directory_reference(monkeypatch, tmp_path):
    """Found in an independent self-review (2026-09-13): the basename-
    containment check (`Path(filename).name != filename`) is meant to
    reject any path-traversal attempt, but `Path("..").name` returns
    `".."` unchanged -- a pathlib quirk, not a slip in `..` specifically
    being special-cased elsewhere -- so the literal two-character string
    `".."` passed the check unmodified. `Path("foo/../bar").name` etc. are
    all correctly caught (any string containing a `/` changes under
    `.name`); only the bare `".."` segment slipped through.

    A literal `..` in the URL is normalized away by any well-behaved HTTP
    client before the request is even sent (confirmed: `httpx.Request`
    resolves `.../evidence/..` to `.../v1` client-side per RFC 3986 dot-
    segment removal) -- so this is reproduced the way a real attacker
    would actually reach it: percent-encoded (`%2e%2e`), which bypasses
    client-side normalization and is decoded back to `..` by Starlette's
    own routing before `filename` is bound. Confirmed against the
    pre-fix code: this reached `FileResponse` with a *directory* path
    (`evidence_dir`'s parent) and raised an unhandled `RuntimeError`
    ("... is not a file"), an uncaught-exception/500 path, not a clean
    400 -- the check's own claim ("prevents ... from exposing arbitrary
    paths") was not accurate for this input, even though FastAPI's
    default `{filename}: str` path converter (no literal `/` possible)
    bounds the real damage to a crash, never a chosen file's content: a
    real file living just outside `evidence_dir` proves that content is
    still never disclosed, before or after this fix.
    """
    monkeypatch.setenv("DATA_DIR", str(ROOT / "demo_data"))
    monkeypatch.setenv("IFC_FILE", "armie_demo.ifc")
    monkeypatch.setenv("PDF_FILES", '["armie_demo_schedule.pdf"]')
    monkeypatch.setenv("AUDIT_STORE_PATH", str(tmp_path / "audit.jsonl"))
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setenv("EVIDENCE_DIR", str(evidence_dir))
    get_settings.cache_clear()

    evidence_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside-evidence-dir-secret.txt").write_bytes(b"should never be servable")
    fake_client = FakeBlobContainerClient()

    import app.main as main_module

    # raise_server_exceptions=False: pre-fix, this reaches an unhandled
    # RuntimeError (FileResponse refusing to serve a directory), which
    # TestClient's own default (True) would instead re-raise in this test
    # process -- False sees the same 500 response shape a real deployed
    # app's ServerErrorMiddleware would actually return to a caller.
    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        main_module.app.state.container.blob_container_client_factory = lambda s: fake_client

        # Percent-encoded, not a literal "..": see the docstring above --
        # a literal ".." never reaches the server at all, normalized away
        # client-side before this test could exercise the real defect.
        response = client.get("/api/v1/evidence/%2e%2e")

        assert response.status_code == 400
        assert b"should never be servable" not in response.content

    get_settings.cache_clear()
