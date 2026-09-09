"""Postgres-backed persistence tests (SPEC-M4 §F).

Gated behind TEST_DATABASE_URL, consistent with this repo's no-network-
egress CI policy (docs/decisions/README.md D-007's seam-test-not-bypass
discipline: CI exercises persistence/factory.py's *selection* logic with no
real connection; these tests exercise the real Postgres-backed
implementations, but only when a developer has opted in by running
`docker compose up postgres` and applying
apps/api/migrations/0001_conversations_and_audit_events.sql -- see
docker-compose.yml and README).

test_a_second_independent_store_instance_reads_back_what_the_first_wrote is
the literal reproduction of D-012 Finding 9 ("audit/evidence does not
persist across a Container App revision replacement"): two independently
constructed store instances stand in for two container processes (a fresh
process after a revision replacement never shares Python object identity
with the one before it), so this could not pass by accident the way a
same-instance round-trip test could.
"""

from __future__ import annotations

import os

import pytest
from app.schemas.models import AuditEvent

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a local Postgres (see docker-compose.yml) to run these",
)


def _stores():
    from app.persistence.postgres_store import (
        PostgresAuditStore,
        PostgresConversationStore,
    )

    return (
        PostgresConversationStore(TEST_DATABASE_URL),
        PostgresAuditStore(TEST_DATABASE_URL),
    )


@pytest.fixture(autouse=True)
def _clean_tables():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE conversations, audit_events")
        conn.commit()
    yield


def test_a_second_independent_store_instance_reads_back_what_the_first_wrote() -> None:
    conversations_1, audit_1 = _stores()
    conversations_1.set("thread-repro", {"active_storey": "Level 02"})
    audit_1.append(AuditEvent(
        trace_id="trace-repro", thread_id="thread-repro", step="route",
        event_type="route_selected", summary="repro",
    ))
    del conversations_1, audit_1  # drop all in-process references; nothing kept them alive but the database

    conversations_2, audit_2 = _stores()

    assert conversations_2.get("thread-repro") == {"active_storey": "Level 02"}
    events = audit_2.by_trace("trace-repro")
    assert len(events) == 1
    assert events[0].event_type == "route_selected"
    assert events[0].summary == "repro"


def test_conversation_store_returns_none_for_an_unknown_thread() -> None:
    conversations, _ = _stores()

    assert conversations.get("thread-never-seen") is None


def test_audit_store_by_trace_only_returns_matching_events() -> None:
    _, audit = _stores()
    audit.append(AuditEvent(trace_id="trace-a", thread_id="t", step="route", event_type="route_selected", summary="a"))
    audit.append(AuditEvent(trace_id="trace-b", thread_id="t", step="route", event_type="route_selected", summary="b"))

    result = audit.by_trace("trace-a")

    assert len(result) == 1
    assert result[0].trace_id == "trace-a"
