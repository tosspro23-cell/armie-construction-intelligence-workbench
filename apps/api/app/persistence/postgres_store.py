from __future__ import annotations

import json
import threading
import time
from typing import Any

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.schemas.models import AuditEvent

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
