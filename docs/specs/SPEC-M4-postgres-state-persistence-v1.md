# SPEC-M4 — Postgres-backed conversation and audit persistence

## Objective

Make `app.state.conversations` (per-thread conversation context) and `AuditStore` (the
append-only event log behind `/api/v1/traces/{trace_id}`) survive an Azure Container App
revision replacement, by backing both with Azure Database for PostgreSQL instead of an
in-process dict and a local JSONL file. This is Phase 2 of the Azure roadmap set up in
SPEC-M3 (OD-20: "PostgreSQL, not Cosmos DB, for state persistence"), and directly closes
Finding 9 of the SPEC-M3 independent review (`docs/decisions/README.md` D-012,
`docs/reports/2026-09-09-m3-independent-review-and-fixes.md`): "audit/evidence does not
persist across a Container App revision replacement."

## Rationale

Today, both stores are process-local:

- `app.state.conversations` is a plain `dict`, created empty in `main.py`'s `lifespan` on
  every process start (`apps/api/app/main.py:30`). A new revision, a crash-restart, or
  (if OD-22's single-replica pin is ever lifted) routing a follow-up request to a
  different replica all silently lose every in-flight conversation's context — the user's
  next message is treated as a fresh thread with no memory of `previous_query_plan`,
  `active_filters`, etc.
- `AuditStore` (`apps/api/app/audit/store.py`) appends `AuditEvent` rows to a local JSONL
  file inside the container's own (non-persistent) filesystem. Every audit trail — this
  system's primary evidence/verification record, see D-010 — is lost the same way.

Neither gap was in SPEC-M3's scope (a vertical-slice deploy, explicitly excluding
persistence — SPEC-M3 §5), and both were correctly left open rather than patched over.
This spec is that follow-up milestone.

## Verified current-state assumptions

Checked directly against the current code on `main` before writing this spec (this
project's own tech-debt-scan discipline, matching how SPEC-M3 began):

- `apps/api/app/main.py:30-31` — `app.state.conversations = {}` and
  `app.state.requests = {}` are both created fresh in `lifespan`, never read from or
  written to any durable store.
- `apps/api/app/main.py:139` — every `/api/v1/chat` call stores
  `record = {"status": ..., "stage": ..., "task": asyncio.current_task(), "trace_id": ...}`
  in `app.state.requests[request_id]`. **The `"task"` field is a live `asyncio.Task`
  object reference**, used by `POST /api/v1/requests/{id}/cancel` (`main.py:197-206`) to
  call `task.cancel()` directly on the in-process coroutine. This is not serializable and
  has no meaningful cross-process representation — a `Task` object only means something
  inside the event loop that created it. **`app.state.requests` is therefore explicitly
  out of scope for this milestone** (see Explicitly excluded scope below); only
  `app.state.conversations` and `AuditStore` move to Postgres.
- `apps/api/app/services.py:31-33` — `ServiceContainer.__init__` constructs
  `AuditStore(settings.audit_store_path)` directly. Unlike the `ModelProvider` seam
  (D-007: `text_provider_factory`/`vision_provider_factory`/`escalation_provider_factory`
  parameters), there is currently no injection seam for the audit store or conversation
  store — tests that touch `ServiceContainer` always get a real `AuditStore` writing to a
  `tmp_path`. This spec adds the equivalent seam, for the same reason D-007 added one for
  providers: so Postgres-backed behaviour can be tested without a real database connection
  in CI (this repo's documented no-network-egress CI policy), and so the fake/local path
  stays exercised by the same tests as production, not bypassed.
- `apps/api/app/config.py:67` — `checkpoint_db_path: Path = Path("./runtime/checkpoints.sqlite")`
  is dead configuration (`docs/decisions/REVIEW_REQUIRED.md`, "M3: `checkpoint_db_path` is
  dead configuration" — flagged, not fixed, in SPEC-M3, with the note that "a future
  milestone should either remove it as dead weight, or decide this is where a real
  checkpointer belongs"). This spec **removes** it rather than repurposes it: it was a
  LangGraph `SqliteSaver`-style checkpoint path, a different mechanism from the bespoke
  `ConversationStore`/`AuditStore` interfaces this spec adds. Reusing the name for an
  unrelated thing would be more confusing than deleting four lines.
- No Postgres client library (`asyncpg`, `psycopg`, or an ORM) is currently a dependency of
  `apps/api` (`apps/api/pyproject.toml`). None of `SQLAlchemy`/`alembic` is present either
  — this project's dependency list is hand-picked and minimal throughout (no ORM even for
  the existing JSONL store); this spec continues that pattern rather than introducing one.
- `infra/bicep/platform.bicep` and `apps.bicep` provision no database resource today.
  Azure Database for PostgreSQL Flexible Server is **not created by this spec's Bicep
  changes without a separate, explicit go-ahead** — see the cost note under Owner
  decisions; the code/schema/tests below can be built and fully verified against a local
  Postgres (or the existing in-memory/file fallback) with zero Azure spend before that
  decision is needed.

## Allowed scope

**A. `ConversationStore` and `AuditStore` as explicit interfaces, each with two
implementations.**
Introduce `apps/api/app/persistence/` with:
- `ConversationStore` (`get(thread_id) -> dict | None`, `set(thread_id, context: dict) -> None`)
  and `InMemoryConversationStore` (wraps today's plain dict — this becomes the local-dev
  default, not a new behaviour).
- `AuditStore` keeps its exact existing two-method interface (`append`, `by_trace`) so
  `main.py`/`graph.py`/`services.py` call sites do not change; the current file-backed
  class becomes `JsonlAuditStore`, moved into `persistence/` unchanged in behaviour.
- `PostgresConversationStore` and `PostgresAuditStore`, same interfaces, backed by two
  tables (see §C).

**B. Factory-injection seam in `ServiceContainer`, mirroring D-007.**
`ServiceContainer.__init__` gains `conversation_store_factory`/`audit_store_factory`
parameters (default: production factories in a new `persistence/factory.py`, selecting
Postgres vs. in-memory/JSONL the same way `providers/factory.py` selects `llm_provider`
branches). `main.py`'s `lifespan` reads conversation context through
`app.state.container.conversation_store` instead of a bare dict; `app.state.conversations`
is removed. `app.state.requests` (Task-cancellation bookkeeping) is untouched — see
Explicitly excluded scope.

**C. Config surface and schema.**
- New settings: `database_url: str | None` (a `postgresql://` DSN; `None` keeps today's
  in-memory/JSONL behaviour — same opt-in pattern as `otel_exporter_connection_string`
  and `ollama_escalation_model`) and `database_use_managed_identity: bool = False`.
- When `database_use_managed_identity` is set, the Postgres connection authenticates with
  an Entra ID access token obtained via `DefaultAzureCredential` (same credential path as
  `AzureOpenAIProvider`) instead of a password in `database_url` — continuing OD-23's
  zero-stored-secret posture for this project's second Azure-managed resource, not just
  its first.
- Remove `checkpoint_db_path` from `Settings` and `.env.example`.
- Two tables, created by a plain `.sql` migration file (no migration framework — consistent
  with "no ORM" above; a single additive file is enough for two tables):
  ```sql
  CREATE TABLE conversations (
      thread_id   TEXT PRIMARY KEY,
      context     JSONB NOT NULL,
      updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE TABLE audit_events (
      id          UUID PRIMARY KEY,
      trace_id    TEXT NOT NULL,
      thread_id   TEXT NOT NULL,
      "timestamp" TIMESTAMPTZ NOT NULL,
      step        TEXT NOT NULL,
      event_type  TEXT NOT NULL,
      summary     TEXT,
      payload     JSONB NOT NULL DEFAULT '{}'::jsonb
  );
  CREATE INDEX audit_events_trace_id_idx ON audit_events (trace_id);
  ```
  (`AuditEvent`'s full field set matches `apps/api/app/schemas/models.py:317-337`; anything
  not broken out into a column lives in `payload`.)

**D. Local development path is unchanged.**
Running the API locally with no `DATABASE_URL` set continues to use
`InMemoryConversationStore` + `JsonlAuditStore` exactly as today — no developer is required
to run Postgres to work on this codebase. A `docker-compose.yml` service for local Postgres
is added for anyone who wants to exercise the Postgres path locally, but it is opt-in.

**E. Dependencies.**
Add `asyncpg` to `apps/api/pyproject.toml` (async, no ORM, smallest dependency that can
execute the raw SQL above and matches this project's existing all-`async`/`await` FastAPI
handlers).

**F. Tests.**
- `PostgresConversationStore`/`PostgresAuditStore` get unit tests against a real local
  Postgres, gated behind an environment variable (e.g. skipped unless `TEST_DATABASE_URL`
  is set) — consistent with this repo's no-network-egress CI policy; CI runs the
  `InMemoryConversationStore`/`JsonlAuditStore` tests (already covered) plus the new
  factory-selection tests (does `persistence/factory.py` pick the right implementation for
  a given `database_url`/`database_use_managed_identity` combination) with zero real
  connections, the same seam-test-not-bypass discipline as `test_provider_seam_invariance.py`.
- A reproduction proving the actual defect this spec fixes: start two independent
  `ServiceContainer`s (simulating two container revisions / two process restarts) pointed
  at the same Postgres, write a conversation context and an audit event through the first,
  and assert the second reads them back — the equivalent of D-012 Finding 9 actually being
  gone, not just "the class has tests."

**G. Infra and deploy workflow (code only — see Owner decisions before this runs against a
real subscription).**
- `infra/bicep/data.bicep`: Azure Database for PostgreSQL Flexible Server, Burstable
  `B1ms`, Entra ID authentication configured (the API's existing managed identity as an AD
  admin/user on the server, no admin password stored anywhere), private/firewall-restricted
  to the Container Apps environment's outbound IP or VNet integration — no public internet
  access.
- `apps.bicep`/`azure-deploy.yml` gain the new `database_url`/managed-identity settings,
  threaded through the same env-var mechanism already used for
  `azure_openai_text_deployment` etc.
- The post-deploy smoke test (already in `azure-deploy.yml` from SPEC-M3) is extended to
  assert a conversation follow-up question keeps context across the call — the actual
  user-visible behaviour this spec is for.

**H. Documentation.**
`AGENT_HANDOFF.md` code map, `docs/decisions/README.md` (new `D-013`), `PROJECT_STATE.md`.

## Explicitly excluded scope

- **Evidence/document file storage** (rendered PDF crops, uploaded IFC files) — needs Azure
  Data Lake Storage Gen2, a materially larger milestone. Already confirmed with the owner
  as out of scope for this spec.
- **`app.state.requests` / cross-process cancellation.** As established above, the current
  cancellation mechanism is a direct reference to a live `asyncio.Task`, which cannot be
  moved to Postgres (or any external store) without redesigning cancellation itself —
  e.g. a polling-based `cancel_requested` flag each replica's own request loop checks,
  instead of one replica reaching into another's event loop. That redesign is real work
  and is not bundled into this spec. **Consequence: OD-22's `minReplicas: maxReplicas: 1`
  pin is not lifted by this milestone.** Persisting conversations/audit durably is valuable
  on its own (revision replacements and crash-restarts happen even at one replica) and is
  a prerequisite for ever lifting OD-22, but is not sufficient by itself.
- **Public API authentication/authorization/rate limiting** (D-012 Finding 1,
  `docs/decisions/REVIEW_REQUIRED.md`) — unrelated architectural gap, its own milestone.
- **The `asyncio.to_thread`/`asyncio.wait_for` cancellation-doesn't-stop-the-thread gap**
  (D-012, "what was deliberately not fixed") — pre-existing, unrelated to persistence.
- **An ORM or migration framework** — two tables, added additively, do not justify one; if
  a third table or a real migration history is ever needed, that is its own decision.

## Affected surfaces

`apps/api/app/main.py`, `apps/api/app/services.py`, `apps/api/app/config.py`,
`apps/api/app/audit/store.py` (relocated), new `apps/api/app/persistence/`,
`apps/api/pyproject.toml`, `.env.example`, `infra/bicep/data.bicep` (new),
`infra/bicep/apps.bicep`, `.github/workflows/azure-deploy.yml`, `docker-compose.yml` (new,
optional local dev), `AGENT_HANDOFF.md`, `docs/decisions/README.md`, `PROJECT_STATE.md`.

## Invariants

All SPEC-M1/M2/M3 invariants continue to apply unchanged (typed planning, capability
gates, evidence + independent verification, honest disposition taxonomy,
deterministic-first computation). New to this spec:

- `ConversationStore`/`AuditStore` are Protocols with exactly two production
  implementations each (in-memory/JSONL, Postgres); no third implementation or
  general-purpose persistence abstraction is introduced (matches D-007's provider-seam
  discipline: the seam changes *who calls* the store, never adds unrelated flexibility).
- No Postgres credential is ever stored in application config, a container image, or an
  IaC template when `database_use_managed_identity` is set — continuing OD-23's posture
  onto this project's second Azure-managed data resource.
- `AuditStore`'s existing two-method interface (`append`, `by_trace`) does not change
  shape; callers in `main.py`/`graph.py` require no changes beyond how the instance is
  constructed.

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including new persistence tests.
- The two-process reproduction in §F demonstrates conversation context and an audit event
  written by one `ServiceContainer` instance are read back by an independently constructed
  second instance pointed at the same Postgres — the literal defect (D-012 Finding 9)
  reproduced as gone, not inferred from unit tests of the class in isolation.
- `ruff check --select F,E9,I,F401 apps/api tests` passes.
- `checkpoint_db_path`/`CHECKPOINT_DB_PATH` no longer appear anywhere in the codebase.
- If deployed (pending the Owner decision below): a real follow-up chat request against the
  live Container App, after a manual revision restart in between the two calls, retains
  conversation context across that restart — the actual production symptom this spec
  exists to fix.

## Documentation requirements

`D-013` (architecture decision record) recording: the `ConversationStore`/`AuditStore`
seam and its two implementations; the `checkpoint_db_path` removal and why (not a
repurpose); the explicit `app.state.requests`/cancellation non-serializability boundary and
its consequence for OD-22. `PROJECT_STATE.md` M4 milestone entry. `REVIEW_REQUIRED.md`'s
`checkpoint_db_path` entry marked resolved (removed, not wired up).

## Git / stop conditions

One commit per subsection (A–H), spec committed alone first per this repo's standard
workflow. Branch: `feat/m4-postgres-persistence`. Stop and report rather than proceeding
if: a third store implementation turns out to be needed; `AuditEvent`'s schema needs a
shape change beyond adding the two indexed/typed columns above (the `payload` JSONB
column should absorb any evolution); or the two-process reproduction in §F fails to show
the defect existed before the fix (if the in-memory dict path already happened to preserve
state some other way, that invalidates this spec's own rationale and needs to be reported,
not worked around).

## Owner decisions

- **OD-24 (confirmed by owner before this spec was written).** Phase 2 scope is
  conversation state + audit records only. Evidence/document file storage is excluded and
  deferred to a separate, later, larger milestone that will need Azure Data Lake Storage
  Gen2.
- **OD-25 (this spec's default, flagged for owner review, not blocking the code work).**
  This milestone does **not** lift OD-22's single-replica pin. `app.state.requests`
  (live `asyncio.Task` references used for cancellation) stays in-memory and
  single-replica-only; cross-replica cancellation is a separate, unscheduled redesign.
  Accepting this means: durable conversations/audit ship now; horizontal scaling still
  waits on that separate redesign.
- **OD-26 (this spec's default, flagged for owner review).** Postgres access uses Entra ID
  / Managed Identity token authentication (`database_use_managed_identity=true` in the
  deployed environment), the same zero-stored-secret posture OD-23 set for Azure OpenAI.
  Local development continues to use a plain local Postgres password (or no Postgres at
  all, via the in-memory/JSONL default) — Managed Identity has no meaning outside Azure.
- **OD-27 (this spec's default, flagged for owner review).** The dead `checkpoint_db_path`
  setting is removed outright, not repurposed as this milestone's persistence path (see
  Verified current-state assumptions).
- **Cost note, not yet an owner decision — needs a yes before any `az deployment` runs
  the Bicep in §G against a real subscription.** Azure Database for PostgreSQL Flexible
  Server (Burstable `B1ms`, the cheapest tier) runs on the order of USD 12–20/month
  **always on** — unlike Container Apps, which the current setup can (and does, via
  `minReplicas`) scale down, Postgres Flexible Server bills continuously unless manually
  stopped (and Azure auto-restarts a manually-stopped server after 7 days). Given the
  unexpected ~$30 spend already seen this session, all of §A–F (code, schema, tests) can be
  built and fully verified against a local Postgres (`docker-compose.yml`) with **zero**
  additional Azure cost; §G's actual cloud deployment should wait for an explicit go-ahead
  once the code is ready, not be assumed as part of "proceed with the spec."
