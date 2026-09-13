-- D-023 addendum: independent-review finding (P1 #1), confirmed live
-- 2026-09-13. 0003_thread_projects.sql only ever created the table --
-- it never backfilled a row for any thread_id that already had a
-- `conversations` row from before this table existed (every real
-- conversation this deployment served from M3 through M8, i.e. before
-- SPEC-M9 shipped bind_project at all). ConversationStore.bind_project's
-- claim-or-read semantics (INSERT ... ON CONFLICT DO UPDATE SET
-- thread_id = thread_id RETURNING project_id) only protect a thread once
-- it already has a thread_projects row: for one of these orphaned
-- threads, the *first* chat() call after this milestone deployed would
-- have performed a plain INSERT, silently binding that thread to
-- whatever project_id the caller happened to request -- carrying its
-- pre-M9 (implicitly "demo") conversation context into a project it was
-- never actually about.
--
-- Every one of these orphaned threads predates ADLS mode existing at
-- all, so "demo" is not a guess -- it is the only project any of them
-- could ever have been. One-time, idempotent (ON CONFLICT DO NOTHING:
-- never overwrites a real binding a thread may have already earned by
-- the time this runs).
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0005_backfill_thread_projects_demo.sql
INSERT INTO thread_projects (thread_id, project_id)
SELECT thread_id, 'demo' FROM conversations
ON CONFLICT (thread_id) DO NOTHING;
