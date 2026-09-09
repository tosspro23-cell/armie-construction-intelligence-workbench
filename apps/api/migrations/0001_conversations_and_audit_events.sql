-- SPEC-M4 §C: schema for PostgresConversationStore / PostgresAuditStore
-- (apps/api/app/persistence/postgres_store.py). Applied by hand today (a
-- single additive file, no migration framework -- SPEC-M4 explicitly
-- excludes introducing an ORM/migration tool for two tables); a real
-- migration history only becomes worth adopting if a third table or actual
-- schema evolution shows up.
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0001_conversations_and_audit_events.sql
-- Azure: run once against the provisioned Flexible Server before the first
-- deploy that sets DATABASE_URL (infra/bicep/data.bicep provisions the
-- server itself, not its schema).

CREATE TABLE IF NOT EXISTS conversations (
    thread_id   TEXT PRIMARY KEY,
    context     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          UUID PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    "timestamp" TIMESTAMPTZ NOT NULL,
    step        TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    summary     TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_events_trace_id_idx ON audit_events (trace_id);
