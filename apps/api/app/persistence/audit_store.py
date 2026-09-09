from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Protocol

from app.schemas.models import AuditEvent


class AuditStore(Protocol):
    """The append-only audit trail behind ``/api/v1/traces/{trace_id}`` (D-010).

    ``JsonlAuditStore`` is this project's original implementation (was
    ``app.audit.store.AuditStore``, moved here unchanged in behaviour --
    SPEC-M4 §A), kept as the local-development/test default.
    ``PostgresAuditStore`` (persistence/postgres_store.py) is the
    production-durable implementation. Both satisfy this exact two-method
    interface so call sites in main.py/graph.py never change.
    """

    def append(self, event: AuditEvent) -> AuditEvent: ...

    def by_trace(self, trace_id: str) -> list[AuditEvent]: ...


class JsonlAuditStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return event

    def by_trace(self, trace_id: str) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        events: list[AuditEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("trace_id") == trace_id:
                    events.append(AuditEvent.model_validate(item))
        return events
