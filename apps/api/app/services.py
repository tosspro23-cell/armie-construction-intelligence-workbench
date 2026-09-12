import asyncio
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.adls import get_datalake_client
from app.config import Settings
from app.evidence_storage import get_blob_container_client
from app.persistence.audit_store import AuditStore
from app.persistence.conversation_store import ConversationStore
from app.persistence.factory import get_audit_store, get_conversation_store
from app.providers.base import EmbeddingProvider, ModelProvider
from app.providers.factory import (
    get_embedding_provider,
    get_escalation_provider,
    get_text_provider,
    get_vision_provider,
)
from app.retrieval import get_search_client
from app.tools.document.analyzer import DocumentAnalyzer
from app.tools.ifc.repository import IfcRepository

TextProviderFactory = Callable[[Settings], ModelProvider]
VisionProviderFactory = Callable[[Settings], ModelProvider]
EscalationProviderFactory = Callable[[Settings], "ModelProvider | None"]
EmbeddingProviderFactory = Callable[[Settings], "EmbeddingProvider | None"]
SearchClientFactory = Callable[[Settings], "object | None"]
BlobContainerClientFactory = Callable[[Settings], "object | None"]
DataLakeClientFactory = Callable[[Settings], "object | None"]
ConversationStoreFactory = Callable[[Settings], ConversationStore]
AuditStoreFactory = Callable[[Settings], AuditStore]


class ProjectNotFoundError(LookupError):
    """SPEC-M9: an unknown `project_id`, or a known one that is not valid
    in the deployment's current mode (e.g. any non-"demo" project when
    `adls_account_url` is unset). Always a 404 at the API boundary
    (app/main.py) -- never a silent fallback to "demo".
    """


@dataclass(frozen=True)
class SourceManifest:
    """SPEC-M9 SS B/F: one project's entry from the committed registry
    (`demo_data/projects_registry.json`) -- a frozen source set (OD-41),
    not a live/mutable description. `file_hashes` maps a bare filename
    (the same names in `ifc_file`/`pdf_files`) to its recorded
    `content_sha256`, checked before any downloaded file is published to a
    project's local cache.
    """

    project_id: str
    display_name: str
    source_set_id: str
    ifc_file: str
    pdf_files: list[str]
    file_hashes: dict[str, str]


def load_projects_registry(path: Path) -> dict[str, SourceManifest]:
    raw = json.loads(path.read_text())
    return {
        project_id: SourceManifest(
            project_id=project_id,
            display_name=entry["display_name"],
            source_set_id=entry["source_set_id"],
            ifc_file=entry["ifc_file"],
            pdf_files=list(entry["pdf_files"]),
            file_hashes={name: info["content_sha256"] for name, info in entry["files"].items()},
        )
        for project_id, entry in raw.items()
    }


class ProjectResources:
    """SPEC-M9: the per-project analogue of `ServiceContainer.ifc_repository`/
    `document_analyzers` -- resolved per `project_id` (via
    `ServiceContainer.get_project`) and passed explicitly to each call site
    that needs it, never read off a shared mutable field. Carries its own
    `manifest` so a call site can tag audit events/evidence locators with
    `project_id`/`source_set_id` without a second lookup.
    """

    def __init__(
        self, ifc_repository: IfcRepository, document_analyzers: dict[str, DocumentAnalyzer], manifest: SourceManifest,
    ) -> None:
        self.ifc_repository = ifc_repository
        self.document_analyzers = document_analyzers
        self.manifest = manifest

    @property
    def document_analyzer(self) -> DocumentAnalyzer:
        return next(iter(self.document_analyzers.values()))


class ServiceContainer:
    """Wires runtime services and non-deterministic provider factories.

    Provider *factories*, not provider instances, are injected here (SPEC-M1
    §4.2/8): selection logic stays centralized in ``providers/factory.py``,
    this only changes who calls the factory. Defaults are the production
    factories; tests substitute the fake provider (D-007) by passing a
    different factory, never by monkeypatching the factory module.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        text_provider_factory: TextProviderFactory = get_text_provider,
        vision_provider_factory: VisionProviderFactory = get_vision_provider,
        escalation_provider_factory: EscalationProviderFactory = get_escalation_provider,
        embedding_provider_factory: EmbeddingProviderFactory = get_embedding_provider,
        search_client_factory: SearchClientFactory = get_search_client,
        blob_container_client_factory: BlobContainerClientFactory = get_blob_container_client,
        datalake_client_factory: DataLakeClientFactory = get_datalake_client,
        conversation_store_factory: ConversationStoreFactory = get_conversation_store,
        audit_store_factory: AuditStoreFactory = get_audit_store,
    ) -> None:
        self.settings = settings
        self.audit_store = audit_store_factory(settings)
        self.conversation_store = conversation_store_factory(settings)
        self.ifc_repository = IfcRepository(settings.ifc_path)
        # SPEC-M6: one DocumentAnalyzer per configured document, keyed by
        # filename, in `settings.pdf_files`' own order -- not filesystem/
        # glob order, which is platform-dependent and would make fixture
        # regression tests non-reproducible. `_execute_pdf` (app/agent/
        # graph.py) is the only multi-document-aware consumer; everything
        # else keeps using `self.document_analyzer` below.
        # SPEC-M8: every analyzer shares the same blob-container-client
        # factory (crop filenames are already globally unique via
        # uuid4()) -- bound to `settings` here so DocumentAnalyzer itself
        # stays decoupled from the Settings type.
        self.document_analyzers: dict[str, DocumentAnalyzer] = {
            pdf_path.name: DocumentAnalyzer(
                pdf_path=pdf_path, evidence_dir=settings.evidence_dir,
                blob_container_client_factory=lambda: blob_container_client_factory(settings),
            )
            for pdf_path in settings.pdf_paths
        }
        self.text_provider_factory = text_provider_factory
        self.vision_provider_factory = vision_provider_factory
        self.escalation_provider_factory = escalation_provider_factory
        self.embedding_provider_factory = embedding_provider_factory
        # A factory, called per-retrieval in app/agent/graph.py -- not a
        # stored instance -- mirroring the model-provider factories above
        # rather than the ConversationStore/AuditStore instances, since the
        # Azure SDK's SearchClient has the same per-call-construction
        # precedent as AzureOpenAIProvider's own client (a documented,
        # deliberate inefficiency: PROJECT_STATE.md's "per-call Azure
        # client/credential reuse" deferred item), not a proven-safe-to-
        # share-across-concurrent-requests object.
        self.search_client_factory = search_client_factory
        # Stored (not only bound into each DocumentAnalyzer above) so
        # app/main.py's evidence_file endpoint can honor whatever factory
        # was injected here too -- tests substitute a fake the same way
        # they do for search_client_factory, never by monkeypatching
        # app.evidence_storage's production global (D-007 discipline).
        self.blob_container_client_factory = blob_container_client_factory
        # SPEC-M9: the committed project registry (demo_data/
        # projects_registry.json by default) -- read once at startup, like
        # every other fixture-shape default, not re-read per request.
        self.datalake_client_factory = datalake_client_factory
        self._project_registry = load_projects_registry(settings.projects_registry_path)
        # Keyed by (project_id, source_set_id), not project_id alone, so a
        # future source_set_id bump can never silently serve stale cached
        # content under the same key (SPEC-M9 §F).
        self._project_resources: dict[tuple[str, str], ProjectResources] = {}
        # One lock per project_id: guards the first (download-triggering)
        # access so two concurrent first-requests for the same uncached
        # project await one real download rather than triggering two
        # (SPEC-M9 §C) -- this process already serves concurrent requests
        # today, so OD-22's single-replica pin does not, by itself, make
        # this race impossible the way it might first appear to.
        self._project_locks: dict[str, asyncio.Lock] = {}

    async def get_project(self, project_id: str) -> ProjectResources:
        """SPEC-M9 §C: resolve `project_id` to its `ProjectResources`,
        downloading and verifying from ADLS on first access if configured,
        or reusing the eagerly-built "demo" resources unchanged if not.

        Raises `ProjectNotFoundError` for an unknown project, or any
        project other than "demo" when `adls_account_url` is unset -- never
        a silent fallback to "demo".
        """
        manifest = self._project_registry.get(project_id)
        if manifest is None:
            raise ProjectNotFoundError(project_id)

        if not self.settings.adls_account_url:
            if project_id != "demo":
                raise ProjectNotFoundError(project_id)
            # Local mode: this is not a new code path -- self.ifc_repository/
            # self.document_analyzers are the exact fields every deployment
            # before this milestone already builds and uses; get_project
            # only gives them a name and a manifest, not new behaviour.
            return ProjectResources(self.ifc_repository, self.document_analyzers, manifest)

        cache_key = (project_id, manifest.source_set_id)
        cached = self._project_resources.get(cache_key)
        if cached is not None:
            return cached

        lock = self._project_locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            # Re-check after acquiring the lock: a concurrent request may
            # have already completed the download while this one waited.
            cached = self._project_resources.get(cache_key)
            if cached is not None:
                return cached
            resources = await asyncio.to_thread(self._load_project_sync, project_id, manifest)
            self._project_resources[cache_key] = resources
            return resources

    def _load_project_sync(self, project_id: str, manifest: SourceManifest) -> ProjectResources:
        cache_dir = self.settings.project_cache_dir / project_id / manifest.source_set_id
        if not cache_dir.exists():
            self._download_and_publish(project_id, manifest, cache_dir)
        ifc_repository = IfcRepository(cache_dir / "ifc" / manifest.ifc_file)
        document_analyzers = {
            pdf_file: DocumentAnalyzer(
                pdf_path=cache_dir / "pdf" / pdf_file, evidence_dir=self.settings.evidence_dir,
                blob_container_client_factory=lambda: self.blob_container_client_factory(self.settings),
            )
            for pdf_file in manifest.pdf_files
        }
        return ProjectResources(ifc_repository, document_analyzers, manifest)

    def _download_and_publish(self, project_id: str, manifest: SourceManifest, cache_dir: Path) -> None:
        """Download every file for `project_id` into a temporary directory,
        verify each against `manifest.file_hashes`, and only then atomically
        rename it into `cache_dir` -- a failed or interrupted download is
        never cached, never partially published (SPEC-M9 §C's explicit
        failure contract, addressing the gap an independent review of this
        spec's first draft correctly flagged as unspecified).
        """
        client = self.datalake_client_factory(self.settings)
        if client is None:
            raise RuntimeError(
                "adls_account_url is set but the Data Lake client factory returned None -- this should not "
                "be reachable; get_project already checked adls_account_url before calling this."
            )
        tmp_dir = Path(tempfile.mkdtemp(dir=str(self.settings.project_cache_dir)))
        try:
            ifc_dest = tmp_dir / "ifc" / manifest.ifc_file
            ifc_dest.parent.mkdir(parents=True, exist_ok=True)
            self._download_verified(client, project_id, "ifc", manifest.ifc_file, manifest.file_hashes[manifest.ifc_file], ifc_dest)
            for pdf_file in manifest.pdf_files:
                # pdf_file may itself carry a subdirectory (e.g. SPEC-M6's
                # "corpus/rfi_log_047.pdf", matching Settings.pdf_files'
                # own relative-path convention) -- mkdir per-file, not
                # once for a flat "pdf/" directory, found live when a
                # registry entry first carried a nested path.
                pdf_dest = tmp_dir / "pdf" / pdf_file
                pdf_dest.parent.mkdir(parents=True, exist_ok=True)
                self._download_verified(client, project_id, "pdf", pdf_file, manifest.file_hashes[pdf_file], pdf_dest)
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir.rename(cache_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    @staticmethod
    def _download_verified(client: object, project_id: str, subdir: str, filename: str, expected_sha256: str, dest: Path) -> None:
        remote_path = f"{project_id}/{subdir}/{filename}"
        data = client.read_file(remote_path)
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"Downloaded {remote_path} does not match the registry's recorded content_sha256 "
                f"(expected {expected_sha256}, got {actual_sha256}) -- refusing to publish a corrupted "
                "or unexpected file."
            )
        dest.write_bytes(data)

    @property
    def document_analyzer(self) -> DocumentAnalyzer:
        """The primary (first-configured) document analyzer.

        Deliberate SPEC-M6 scope boundary, not an oversight: the raw PDF/
        page-image viewer endpoints (app/main.py) and the door/window
        reconciliation pilot (OD-15/D-011, app/agent/graph.py's
        _execute_multi) both stay scoped to one document in this
        milestone -- neither is made multi-document-aware. Only
        `_execute_pdf`'s deterministic lookup fans out across
        `document_analyzers`.
        """
        return next(iter(self.document_analyzers.values()))

    @property
    def project_registry(self) -> dict[str, SourceManifest]:
        """Read-only view of the committed project registry (SPEC-M9), for
        `GET /api/v1/projects` -- never mutated after `__init__` loads it.
        """
        return self._project_registry

    def project_metadata(self) -> dict:
        return {
            "ifc_available": self.settings.ifc_path.exists(),
            "pdf_available": self.document_analyzer.available,
            "ifc_file": self.settings.ifc_file,
            "pdf_file": self.document_analyzer.pdf_path.name,
            "pdf_files": list(self.document_analyzers.keys()),
            "capabilities": {
                "ifc_query": self.settings.ifc_path.exists(),
                "pdf_native_extraction": self.document_analyzer.available,
                "viewer_snapshot": True,
                "providers": ["openai", "ollama", "hybrid", "azure"],
            },
        }

