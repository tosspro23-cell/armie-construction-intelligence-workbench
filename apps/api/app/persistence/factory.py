from __future__ import annotations

from app.config import Settings
from app.persistence.audit_store import AuditStore, JsonlAuditStore
from app.persistence.conversation_store import ConversationStore, InMemoryConversationStore


def get_conversation_store(settings: Settings) -> ConversationStore:
    """Selection logic stays centralized here (mirrors ``providers/factory.py``,
    D-007): the seam in ``ServiceContainer`` changes who calls this function,
    never how it chooses. ``database_url`` unset (the local-development and
    test default) keeps today's in-process behaviour; setting it opts into
    Postgres-backed durability (SPEC-M4).
    """
    if settings.database_url:
        from app.persistence.postgres_store import PostgresConversationStore

        return PostgresConversationStore(settings.database_url, use_managed_identity=settings.database_use_managed_identity)
    return InMemoryConversationStore()


def get_audit_store(settings: Settings) -> AuditStore:
    if settings.database_url:
        from app.persistence.postgres_store import PostgresAuditStore

        return PostgresAuditStore(settings.database_url, use_managed_identity=settings.database_use_managed_identity)
    return JsonlAuditStore(settings.audit_store_path)
