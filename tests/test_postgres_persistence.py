"""Postgres-backed persistence tests (SPEC-M4 §F).

Gated behind TEST_DATABASE_URL, consistent with this repo's no-network-
egress CI policy (docs/decisions/README.md D-007's seam-test-not-bypass
discipline: CI exercises persistence/factory.py's *selection* logic with no
real connection; these tests exercise the real Postgres-backed
implementations, but only when a developer has opted in by running
`docker compose up postgres` and applying apps/api/migrations/0001_
conversations_and_audit_events.sql and 0003_thread_projects.sql -- see
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
from pathlib import Path

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
        # thread_projects (SPEC-M9, 0003_thread_projects.sql): a developer
        # running these tests before 0003 was applied would have neither
        # table -- TRUNCATE fails outright if it's missing, which is a
        # clearer signal than silently skipping it.
        cur.execute("TRUNCATE conversations, audit_events, thread_projects")
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


# --- 0005_backfill_thread_projects_demo.sql (D-023 addendum, independent-review P1 #1) ---
#
# Reproduced and fixed against a real Postgres, not a fake -- this is
# fundamentally a data/migration-sequencing defect, not something a
# fake-store unit test could ever exercise: 0003_thread_projects.sql only
# ever created the table, with no backfill for a thread_id that already
# had a `conversations` row from before that table existed (every real
# conversation this project's deployments served from M3 through M8).

def test_pre_m9_thread_is_silently_rebindable_before_the_backfill_runs() -> None:
    """The defect itself, reproduced directly: a thread with real
    conversation context but no thread_projects row -- exactly what every
    pre-M9 conversation looks like today -- gets silently claimed by
    whatever project the *next* caller happens to request, carrying its
    old (implicitly "demo") context into a project it was never about.
    """
    conversations, _ = _stores()
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (thread_id, context) VALUES (%s, %s)", ("pre-m9-thread", '{"active_source": "pdf"}'))
        conn.commit()

    bound = conversations.bind_project("pre-m9-thread", "westgate")

    assert bound == "westgate"  # the defect: silently claimed, not rejected
    assert conversations.get("pre-m9-thread") == {"active_source": "pdf"}  # ...with its old context intact


def test_the_backfill_migration_closes_it() -> None:
    """Same setup as the defect reproduction above, but with 0005 applied
    first -- the same request now correctly stays bound to "demo" (the
    only project this thread could ever have been), so main.py's own
    project-mismatch check rejects "westgate" with a 409 before any
    tool/model call, instead of silently proceeding under the wrong
    project's data.
    """
    import psycopg
    from app.persistence.postgres_store import PostgresConversationStore

    migration_sql = (Path(__file__).resolve().parents[1] / "apps" / "api" / "migrations" / "0005_backfill_thread_projects_demo.sql").read_text()

    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (thread_id, context) VALUES (%s, %s)", ("pre-m9-thread", '{"active_source": "pdf"}'))
        conn.commit()
        cur.execute(migration_sql)
        conn.commit()

    conversations = PostgresConversationStore(TEST_DATABASE_URL)
    bound = conversations.bind_project("pre-m9-thread", "westgate")

    assert bound == "demo"  # rejected upstream, per main.py's existing 409 check
    assert conversations.get("pre-m9-thread") == {"active_source": "pdf"}  # untouched either way
