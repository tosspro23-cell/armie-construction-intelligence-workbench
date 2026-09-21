from __future__ import annotations

import json
import threading
import time
from typing import Any

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.persistence.finding_store import FindingVersionConflict
from app.schemas.models import (
    AgentProposal,
    AuditEvent,
    EngineeringFinding,
    FindingHistoryEntry,
    FindingStatus,
)

# The Cognitive Services scope reused for Azure OpenAI (SPEC-M3, OD-23) does
# not apply here: an Entra ID access token usable as a Postgres password
# must be scoped to Azure's own OSS RDBMS resource, a fixed, documented
# audience distinct per-service the same way Cognitive Services' is.
_POSTGRES_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# psycopg's sync connections are deliberately used here, not an async driver
# (SPEC-M4 addendum, found while implementing): AuditStore.append() is called
# throughout apps/api/app/agent/graph.py's AgentService._audit(), which runs
# synchronously inside the worker thread main.py's chat() hands work to via
# asyncio.to_thread(agent.invoke, ...) -- there is no event loop in that
# thread for an async-only driver (asyncpg) to run on. A sync driver keeps
# this store callable from exactly the same context AuditStore has always
# been called from; main.py's own async handlers already tolerate a few
# milliseconds of blocking DB I/O per request without a dedicated thread hop,
# consistent with how they call the (equally synchronous) JsonlAuditStore
# today.


def _conninfo_with_password(base_conninfo: str, password: str) -> str:
    """Merge a fresh token into ``base_conninfo`` as its password.

    Not string concatenation: ``database_url`` is a ``postgresql://`` URI
    (documented in .env.example), and a bare ``"password=..."``
    keyword/value fragment does not combine with a URI by string-pasting --
    an earlier version of this module did exactly that and silently
    produced an invalid combined conninfo, never exercised by a test until
    this was run against a real Postgres. ``make_conninfo`` understands
    both URI and keyword/value forms and merges the override correctly
    regardless of which one the base conninfo is in.
    """
    return make_conninfo(base_conninfo, password=password)


# Independent-review finding: none of these were bounded before, so a
# stalled network path or an overloaded server could block a caller
# indefinitely -- and since every store call in main.py's async handlers
# runs via asyncio.to_thread, "indefinitely" meant tying up a worker thread
# from asyncio's shared default executor for that whole time, not just the
# one request that triggered it. connect_timeout bounds the TCP-connect +
# auth handshake; statement_timeout (a session GUC, set via conninfo
# options) bounds any single SQL statement once connected.
_CONNECT_TIMEOUT_SECONDS = 10
_STATEMENT_TIMEOUT_MS = 15_000
# How long a caller waits for a pooled connection to become available
# before psycopg_pool raises PoolTimeout, distinct from connect_timeout
# above (which bounds establishing a brand new connection).
_POOL_WAIT_TIMEOUT_SECONDS = 10.0


def _with_bounded_timeouts(conninfo: str) -> str:
    return make_conninfo(
        conninfo,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
    )


def _build_managed_identity_token_provider() -> Any:
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    def get_token() -> str:
        return credential.get_token(_POSTGRES_AAD_SCOPE).token

    return get_token


class _TokenRefreshingPool:
    """Wraps a psycopg ``ConnectionPool`` whose password is a short-lived
    Entra ID access token (OD-26): tokens expire in roughly an hour, so the
    pool must be rebuilt with a fresh token periodically rather than created
    once at process start. A plain ``ConnectionPool`` has no hook for this;
    this wrapper re-fetches the token and points a new pool at it whenever
    the cached one is older than ``refresh_seconds``, guarded by a lock so
    concurrent requests during a refresh do not each rebuild the pool.
    """

    def __init__(self, conninfo_without_password: str, token_provider, *, refresh_seconds: float = 45 * 60) -> None:
        self._conninfo = conninfo_without_password
        self._token_provider = token_provider
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._pool: ConnectionPool | None = None
        self._issued_at = 0.0

    def _current_pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None or (time.monotonic() - self._issued_at) > self._refresh_seconds:
                # Build and open the replacement pool BEFORE closing the
                # old one (independent review finding): the previous order
                # closed self._pool first, so a token-provider failure or a
                # new-pool connection error left this store with no working
                # pool at all -- destroying a still-valid resource before
                # confirming its replacement actually works. Every request
                # arriving during that window failed even though the old
                # pool would have kept working fine for a while longer.
                token = self._token_provider()
                conninfo = _with_bounded_timeouts(_conninfo_with_password(self._conninfo, token))
                new_pool = ConnectionPool(conninfo, min_size=1, max_size=5, timeout=_POOL_WAIT_TIMEOUT_SECONDS, open=True)
                old_pool, self._pool = self._pool, new_pool
                self._issued_at = time.monotonic()
                if old_pool is not None:
                    old_pool.close()
            return self._pool

    def connection(self):
        return self._current_pool().connection()

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None


def _make_pool(database_url: str, *, use_managed_identity: bool) -> ConnectionPool | _TokenRefreshingPool:
    if not use_managed_identity:
        conninfo = _with_bounded_timeouts(database_url)
        return ConnectionPool(conninfo, min_size=1, max_size=5, timeout=_POOL_WAIT_TIMEOUT_SECONDS, open=True)
    return _TokenRefreshingPool(database_url, _build_managed_identity_token_provider())


class PostgresConversationStore:
    """``ConversationStore`` backed by Azure Database for PostgreSQL (SPEC-M4 §A/C).

    Same two-method interface as ``InMemoryConversationStore`` -- callers in
    main.py never need to know which one is live.
    """

    def __init__(self, database_url: str, *, use_managed_identity: bool = False) -> None:
        self._pool = _make_pool(database_url, use_managed_identity=use_managed_identity)

    def get(self, thread_id: str) -> dict | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT context FROM conversations WHERE thread_id = %s", (thread_id,))
            row = cur.fetchone()
            return row["context"] if row else None

    def set(self, thread_id: str, context: dict) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations (thread_id, context, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (thread_id) DO UPDATE SET context = EXCLUDED.context, updated_at = now()
                """,
                (thread_id, json.dumps(context)),
            )
            conn.commit()

    def bind_project(self, thread_id: str, project_id: str) -> str:
        # A separate table (migrations/0003_thread_projects.sql), not a
        # column on `conversations` -- see conversation_store.py's own
        # docstring for why. The no-op `DO UPDATE SET thread_id =
        # thread_projects.thread_id` is a standard idiom to make
        # `RETURNING` fire on conflict too, so this single round trip both
        # claims an unbound thread and fetches an already-bound one --
        # avoiding a read-then-write race at the database level.
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO thread_projects (thread_id, project_id)
                VALUES (%s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET thread_id = thread_projects.thread_id
                RETURNING project_id
                """,
                (thread_id, project_id),
            )
            row = cur.fetchone()
            conn.commit()
            return row["project_id"]

    def close(self) -> None:
        self._pool.close()


class PostgresAuditStore:
    """``AuditStore`` backed by Azure Database for PostgreSQL (SPEC-M4 §A/C).

    Same two-method interface (``append``, ``by_trace``) as ``JsonlAuditStore``.
    """

    def __init__(self, database_url: str, *, use_managed_identity: bool = False) -> None:
        self._pool = _make_pool(database_url, use_managed_identity=use_managed_identity)

    def append(self, event: AuditEvent) -> AuditEvent:
        payload = json.loads(event.model_dump_json())
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events (id, trace_id, thread_id, "timestamp", step, event_type, summary, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload["id"], payload["trace_id"], payload["thread_id"], payload["timestamp"],
                    payload["step"], payload["event_type"], payload.get("summary"),
                    json.dumps({k: v for k, v in payload.items() if k not in {
                        "id", "trace_id", "thread_id", "timestamp", "step", "event_type", "summary",
                    }}),
                ),
            )
            conn.commit()
        return event

    def by_trace(self, trace_id: str) -> list[AuditEvent]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'SELECT id, trace_id, thread_id, "timestamp", step, event_type, summary, payload '
                "FROM audit_events WHERE trace_id = %s ORDER BY \"timestamp\" ASC",
                (trace_id,),
            )
            rows = cur.fetchall()
        events = []
        for row in rows:
            data = {**row.pop("payload"), **row}
            # psycopg deserializes the UUID/TIMESTAMPTZ columns into native
            # uuid.UUID/datetime objects; AuditEvent.id is a plain str
            # (found running this against a real Postgres -- pydantic
            # rejects a UUID object for a str field even though it accepts
            # a datetime object for a datetime field without complaint).
            data["id"] = str(data["id"])
            events.append(AuditEvent.model_validate(data))
        return events

    def close(self) -> None:
        self._pool.close()


# SPEC-M11 §4B: findings live/resolved/pending-verification still count as
# "the active one for this tag+finding_type" -- matches
# migrations/0006_engineering_findings.sql's own partial unique index
# predicate exactly. Kept as a literal SQL fragment, not built from the
# Python-side ACTIVE_STATUSES constant, so the two never drift silently out
# of sync with each other without a diff showing it.
_ACTIVE_STATUSES_SQL = "('open', 'acknowledged', 'action_required', 'resolved')"


class PostgresFindingStore:
    """``FindingStore`` backed by Azure Database for PostgreSQL (SPEC-M11).

    Same interface as ``InMemoryFindingStore`` -- callers never need to
    know which one is live.
    """

    def __init__(self, database_url: str, *, use_managed_identity: bool = False) -> None:
        self._pool = _make_pool(database_url, use_managed_identity=use_managed_identity)

    def upsert_from_reconciliation(
        self, *, project_id: str, source_set_id: str, trace_id: str, tag: str,
        finding_type: str, severity: str, detail: str,
        ifc_width_m: float | None, ifc_height_m: float | None,
        pdf_width_m: float | None, pdf_height_m: float | None,
        evidence_refs: list[str], entity_type: str | None = None,
    ) -> EngineeringFinding:
        from uuid import uuid4
        finding_id = str(uuid4())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # `(xmax = 0)` is the standard Postgres idiom for "did this
            # INSERT...ON CONFLICT DO UPDATE actually insert a new row, or
            # touch an existing one" -- xmax is unset (0) only for a row
            # this same command just created. Needed here because, unlike
            # the simpler upserts elsewhere in this file, only a genuine
            # *insert* also gets a "Detected by reconciliation" history row.
            cur.execute(
                f"""
                INSERT INTO engineering_findings
                    (finding_id, project_id, source_set_id, trace_id, tag, finding_type, severity,
                     status, detail, ifc_width_m, ifc_height_m, pdf_width_m, pdf_height_m, evidence_refs, entity_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, tag, finding_type) WHERE status IN {_ACTIVE_STATUSES_SQL}
                DO UPDATE SET
                    trace_id = EXCLUDED.trace_id, detail = EXCLUDED.detail,
                    ifc_width_m = EXCLUDED.ifc_width_m, ifc_height_m = EXCLUDED.ifc_height_m,
                    pdf_width_m = EXCLUDED.pdf_width_m, pdf_height_m = EXCLUDED.pdf_height_m,
                    evidence_refs = EXCLUDED.evidence_refs, entity_type = EXCLUDED.entity_type, updated_at = now()
                RETURNING *, (xmax = 0) AS inserted
                """,
                (
                    finding_id, project_id, source_set_id, trace_id, tag, finding_type, severity,
                    detail, ifc_width_m, ifc_height_m, pdf_width_m, pdf_height_m, json.dumps(evidence_refs),
                    entity_type,
                ),
            )
            row = cur.fetchone()
            was_inserted = row.pop("inserted")
            if was_inserted:
                cur.execute(
                    """
                    INSERT INTO engineering_finding_history (id, finding_id, from_status, to_status, actor_session_id, note)
                    VALUES (%s, %s, NULL, 'open', NULL, 'Detected by reconciliation.')
                    """,
                    (str(uuid4()), row["finding_id"]),
                )
            conn.commit()
        return self.get(row["finding_id"])  # re-read: simplest way to get history consistently populated either branch

    def get(self, finding_id: str) -> EngineeringFinding | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM engineering_findings WHERE finding_id = %s", (finding_id,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT id, from_status, to_status, actor_session_id, note, proposal_snapshot, at "
                "FROM engineering_finding_history WHERE finding_id = %s ORDER BY at ASC",
                (finding_id,),
            )
            history_rows = cur.fetchall()
        return self._to_model(row, history_rows)

    def list_for_project(self, project_id: str, status: FindingStatus | None = None) -> list[EngineeringFinding]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if status is not None:
                cur.execute(
                    "SELECT * FROM engineering_findings WHERE project_id = %s AND status = %s ORDER BY created_at DESC",
                    (project_id, status.value),
                )
            else:
                cur.execute(
                    "SELECT * FROM engineering_findings WHERE project_id = %s ORDER BY created_at DESC",
                    (project_id,),
                )
            rows = cur.fetchall()
        # History is fetched per-finding via get() by callers that need it
        # (the list view itself never needs to render another finding's
        # full history) -- avoids an N+1 history join for what is, in this
        # milestone, a list endpoint that only shows status/detail/severity.
        return [self._to_model(row, []) for row in rows]

    def append_transition(
        self, finding_id: str, *, to_status: FindingStatus, actor_session_id: str | None, note: str | None,
        updates: dict | None = None, proposal_snapshot: AgentProposal | None = None,
        expected_pending_proposal_id: str | None = None,
    ) -> EngineeringFinding:
        """Independent-review finding, 2026-09-19: this used to read
        `existing` via a separate, unlocked `self.get(finding_id)` call,
        then update the row in a second connection/transaction entirely --
        a real TOCTOU window between the two, on top of never checking
        `expected_pending_proposal_id` at all. Now takes a row lock
        (`SELECT ... FOR UPDATE`) inside the same transaction that performs
        the write, so the version check and the update are atomic with
        respect to a concurrent approve/reject/re-investigate on the same
        finding, not just "checked, then hopefully still true."
        """
        from uuid import uuid4
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status, pending_proposal FROM engineering_findings WHERE finding_id = %s FOR UPDATE", (finding_id,))
            locked = cur.fetchone()
            if locked is None:
                raise KeyError(finding_id)
            if expected_pending_proposal_id is not None:
                current_proposal = locked.get("pending_proposal")
                current_id = current_proposal.get("proposal_id") if current_proposal else None
                if current_id != expected_pending_proposal_id:
                    conn.rollback()
                    raise FindingVersionConflict(
                        f"Finding '{finding_id}' pending_proposal has changed since it was loaded "
                        f"(expected proposal '{expected_pending_proposal_id}', current is {current_id!r}) -- "
                        "reload the finding and review its current proposal before approving/rejecting."
                    )
            from_status = locked["status"]
            set_clauses = ["status = %s", "last_actor_session_id = %s", "updated_at = now()"]
            params: list[Any] = [to_status.value, actor_session_id]
            for key, value in (updates or {}).items():
                set_clauses.append(f"{key} = %s")
                params.append(value)
            params.append(finding_id)
            cur.execute(f"UPDATE engineering_findings SET {', '.join(set_clauses)} WHERE finding_id = %s", params)
            cur.execute(
                """
                INSERT INTO engineering_finding_history
                    (id, finding_id, from_status, to_status, actor_session_id, note, proposal_snapshot)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()), finding_id, from_status, to_status.value, actor_session_id, note,
                    json.dumps(proposal_snapshot.model_dump(mode="json")) if proposal_snapshot is not None else None,
                ),
            )
            conn.commit()
        return self.get(finding_id)

    def set_pending_proposal(
        self, finding_id: str, proposal: AgentProposal | None, *, expected_status: FindingStatus | None = None,
    ) -> EngineeringFinding | None:
        """SPEC-M17 §4B: updates only `pending_proposal`/`updated_at` -- no
        history row, no status change (a propose-resolution call is not a
        state-machine transition).

        `expected_status` (independent-review finding, 2026-09-19): row-
        locked and checked in the same transaction as the write -- a
        finding a human resolved/waived while this investigation was
        still running must not have a `pending_proposal` resurrected onto
        it after the fact. Returns `None` (not an exception -- this is a
        routine race, not a caller error) when the check fails; the write
        is skipped entirely.
        """
        payload = json.dumps(proposal.model_dump(mode="json")) if proposal is not None else None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status FROM engineering_findings WHERE finding_id = %s FOR UPDATE", (finding_id,))
            locked = cur.fetchone()
            if locked is None:
                raise KeyError(finding_id)
            if expected_status is not None and locked["status"] != expected_status.value:
                conn.rollback()
                return None
            cur.execute(
                "UPDATE engineering_findings SET pending_proposal = %s, updated_at = now() WHERE finding_id = %s",
                (payload, finding_id),
            )
            conn.commit()
        return self.get(finding_id)

    @staticmethod
    def _to_model(row: dict, history_rows: list[dict]) -> EngineeringFinding:
        return EngineeringFinding(
            finding_id=row["finding_id"], project_id=row["project_id"], source_set_id=row["source_set_id"],
            trace_id=row["trace_id"], tag=row["tag"], finding_type=row["finding_type"], severity=row["severity"],
            status=row["status"], detail=row["detail"], entity_type=row.get("entity_type"),
            ifc_width_m=row["ifc_width_m"], ifc_height_m=row["ifc_height_m"],
            pdf_width_m=row["pdf_width_m"], pdf_height_m=row["pdf_height_m"],
            evidence_refs=row["evidence_refs"], created_at=row["created_at"], updated_at=row["updated_at"],
            last_actor_session_id=row["last_actor_session_id"],
            # SPEC-M17: psycopg's own JSONB adapter already deserializes
            # this column into a plain dict (or None) on fetch -- the same
            # reason `evidence_refs` above needs no manual json.loads --
            # Pydantic then validates that dict straight into AgentProposal.
            pending_proposal=row.get("pending_proposal"),
            history=[
                FindingHistoryEntry(
                    id=str(item["id"]), from_status=item["from_status"], to_status=item["to_status"],
                    actor_session_id=item["actor_session_id"], note=item["note"],
                    proposal_snapshot=item.get("proposal_snapshot"), at=item["at"],
                )
                for item in history_rows
            ],
        )

    def close(self) -> None:
        self._pool.close()
