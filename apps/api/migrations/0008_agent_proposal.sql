-- SPEC-M17: two additive columns for agent-assisted finding resolution.
-- Applied by hand like every migration before it -- still no migration
-- framework (see 0001's own header comment).
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0008_agent_proposal.sql
--
-- Both target tables are fully normalized, column-per-field (only
-- evidence_refs uses JSONB today, scoped to that one list field) -- a new
-- nested AgentProposal object genuinely needs its own column, not a
-- schema-free slot. No grant migration is needed alongside this one:
-- 0007_grant_engineering_findings.sql's GRANTs are table-scoped
-- (SELECT/INSERT/UPDATE on the whole table), which already cover any
-- column added later via ALTER TABLE.
ALTER TABLE engineering_findings
    ADD COLUMN IF NOT EXISTS pending_proposal JSONB;

-- Set only on the one history row an approve_proposal transition creates --
-- an immutable copy of the exact proposal that was approved, independent of
-- engineering_findings.pending_proposal, which a later propose-resolution
-- call can overwrite.
ALTER TABLE engineering_finding_history
    ADD COLUMN IF NOT EXISTS proposal_snapshot JSONB;
