from typing import Callable

from app.config import Settings
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
ConversationStoreFactory = Callable[[Settings], ConversationStore]
AuditStoreFactory = Callable[[Settings], AuditStore]


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
        self.document_analyzers: dict[str, DocumentAnalyzer] = {
            pdf_path.name: DocumentAnalyzer(pdf_path=pdf_path, evidence_dir=settings.evidence_dir)
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

