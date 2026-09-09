"""Unit tests for the local-development persistence implementations
(SPEC-M4 §A): ``InMemoryConversationStore`` and ``JsonlAuditStore``.

These are the exact two implementations that were previously a bare
``dict`` (``app.state.conversations``) and the module formerly at
``app.audit.store.AuditStore``, now given an explicit interface so a
Postgres-backed implementation can be substituted through the same seam
(persistence/factory.py) without changing any call site. No network I/O,
no Postgres required.
"""

from __future__ import annotations

from pathlib import Path

from app.persistence.audit_store import JsonlAuditStore
from app.persistence.conversation_store import InMemoryConversationStore
from app.schemas.models import AuditEvent


def test_in_memory_conversation_store_round_trips_context() -> None:
    store = InMemoryConversationStore()

    assert store.get("thread-1") is None

    store.set("thread-1", {"active_storey": "Level 01"})

    assert store.get("thread-1") == {"active_storey": "Level 01"}


def test_in_memory_conversation_store_keeps_threads_independent() -> None:
    store = InMemoryConversationStore()
    store.set("thread-a", {"value": "a"})
    store.set("thread-b", {"value": "b"})

    assert store.get("thread-a") == {"value": "a"}
    assert store.get("thread-b") == {"value": "b"}


def test_jsonl_audit_store_append_and_by_trace(tmp_path: Path) -> None:
    store = JsonlAuditStore(tmp_path / "audit.jsonl")
    event = AuditEvent(trace_id="trace-1", thread_id="thread-1", step="route", event_type="route_selected", summary="s")

    stored = store.append(event)

    assert stored == event
    assert store.by_trace("trace-1") == [event]


def test_jsonl_audit_store_by_trace_filters_to_the_requested_trace(tmp_path: Path) -> None:
    store = JsonlAuditStore(tmp_path / "audit.jsonl")
    store.append(AuditEvent(trace_id="trace-1", thread_id="t", step="route", event_type="route_selected", summary="s"))
    store.append(AuditEvent(trace_id="trace-2", thread_id="t", step="route", event_type="route_selected", summary="s"))

    result = store.by_trace("trace-1")

    assert len(result) == 1
    assert result[0].trace_id == "trace-1"


def test_jsonl_audit_store_by_trace_is_empty_when_the_file_does_not_exist_yet(tmp_path: Path) -> None:
    store = JsonlAuditStore(tmp_path / "does-not-exist" / "audit.jsonl")

    assert store.by_trace("trace-1") == []
