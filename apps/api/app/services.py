from pathlib import Path

from app.audit.store import AuditStore
from app.config import Settings
from app.tools.document.analyzer import DocumentAnalyzer
from app.tools.ifc.repository import IfcRepository


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit_store = AuditStore(settings.audit_store_path)
        self.ifc_repository = IfcRepository(settings.ifc_path)
        self.document_analyzer = DocumentAnalyzer(
            pdf_path=settings.pdf_path,
            evidence_dir=settings.evidence_dir,
        )

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

