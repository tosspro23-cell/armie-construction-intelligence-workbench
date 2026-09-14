-- SPEC-M11: two additive tables, applied by hand like every migration
-- before it (still no migration framework -- see 0001's own header
-- comment).
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0006_engineering_findings.sql
--
-- The partial unique index below is the actual enforcement of
-- FindingStore.upsert_from_reconciliation's "one active finding per
-- project/tag/finding_type" contract at the database level, not just in
-- application code -- a second reconciliation run for the same still-open
-- mismatch updates the existing row (ON CONFLICT) rather than a Postgres
-- constraint violation or, worse, a silent duplicate row. RESOLVED counts
-- as "active" here too: a finding a human has claimed fixed but that
-- hasn't yet been re-verified must still be the one row a fresh
-- reconciliation run updates, not a second finding for the same mismatch.
CREATE TABLE IF NOT EXISTS engineering_findings (
    finding_id             TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
    source_set_id          TEXT NOT NULL,
    trace_id               TEXT NOT NULL,
    tag                    TEXT NOT NULL,
    finding_type           TEXT NOT NULL,
    severity               TEXT NOT NULL,
    status                 TEXT NOT NULL,
    detail                 TEXT NOT NULL,
    ifc_width_m            DOUBLE PRECISION,
    ifc_height_m           DOUBLE PRECISION,
    pdf_width_m            DOUBLE PRECISION,
    pdf_height_m           DOUBLE PRECISION,
    evidence_refs          JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_actor_session_id  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS engineering_findings_active_key
    ON engineering_findings (project_id, tag, finding_type)
    WHERE status IN ('open', 'acknowledged', 'action_required', 'resolved');

CREATE INDEX IF NOT EXISTS engineering_findings_project_status_idx
    ON engineering_findings (project_id, status);

-- Append-only, per FindingHistoryEntry (apps/api/app/schemas/models.py):
-- no UPDATE/DELETE is ever issued against this table -- the same
-- no-delete discipline 0002b already established for
-- conversations/audit_events.
CREATE TABLE IF NOT EXISTS engineering_finding_history (
    id                TEXT PRIMARY KEY,
    finding_id        TEXT NOT NULL REFERENCES engineering_findings (finding_id),
    from_status       TEXT,
    to_status         TEXT NOT NULL,
    actor_session_id  TEXT,
    note              TEXT,
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS engineering_finding_history_finding_id_idx
    ON engineering_finding_history (finding_id);
