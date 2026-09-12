-- SPEC-M9 SS D: one additive table, applied by hand like 0001/0002a/0002b
-- (still no migration framework -- see 0001's own header comment; a third
-- table does not yet justify adopting one).
--
-- Kept separate from `conversations`, not a new column on it, on purpose:
-- ConversationStore.get()'s None-vs-populated distinction is what
-- main.py's resume() uses to 404 a thread that was never actually
-- completed. Folding project binding into `conversations.context` would
-- mean bind_project's own INSERT pre-creates an "empty" context row the
-- moment a chat request starts, before any real turn completes -- silently
-- breaking that check for a thread whose first request timed out,
-- errored, or was cancelled after binding but before ever calling
-- ConversationStore.set().
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0003_thread_projects.sql
CREATE TABLE IF NOT EXISTS thread_projects (
    thread_id   TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
