from pathlib import Path
from typing import Callable

from app.audit.store import AuditStore
from app.config import Settings
from app.providers.base import ModelProvider
from app.providers.factory import get_escalation_provider, get_text_provider, get_vision_provider
from app.tools.document.analyzer import DocumentAnalyzer
from app.tools.ifc.repository import IfcRepository

TextProviderFactory = Callable[[Settings], ModelProvider]
VisionProviderFactory = Callable[[Settings], ModelProvider]
EscalationProviderFactory = Callable[[Settings], "ModelProvider | None"]


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
    ) -> None:
        self.settings = settings
        self.audit_store = AuditStore(settings.audit_store_path)
        self.ifc_repository = IfcRepository(settings.ifc_path)
        self.document_analyzer = DocumentAnalyzer(
            pdf_path=settings.pdf_path,
            evidence_dir=settings.evidence_dir,
        )
        self.text_provider_factory = text_provider_factory
        self.vision_provider_factory = vision_provider_factory
        self.escalation_provider_factory = escalation_provider_factory

    def project_metadata(self) -> dict:
        return {
            "ifc_available": self.settings.ifc_path.exists(),
            "pdf_available": self.settings.pdf_path.exists(),
            "ifc_file": self.settings.ifc_file,
            "pdf_file": self.settings.pdf_file,
            "capabilities": {
                "ifc_query": self.settings.ifc_path.exists(),
                "pdf_native_extraction": self.settings.pdf_path.exists(),
                "viewer_snapshot": True,
                "providers": ["openai", "ollama", "hybrid"],
            },
        }

