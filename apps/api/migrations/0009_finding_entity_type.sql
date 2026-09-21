-- D-070: one additive column so a persisted finding carries the IFC
-- entity type (IfcDoor/IfcWindow) reconciliation already determined it
-- from, instead of the "investigate this" question leaving the model to
-- guess which type to query. Applied by hand like every migration before
-- it -- still no migration framework (see 0001's own header comment).
--
-- Local dev: psql "$DATABASE_URL" -f apps/api/migrations/0009_finding_entity_type.sql
--
-- No grant migration needed alongside this one: 0007_grant_engineering_
-- findings.sql's GRANTs are table-scoped (SELECT/INSERT/UPDATE on the
-- whole table), which already cover any column added later via ALTER
-- TABLE -- the same reasoning 0008's own header comment already recorded.
ALTER TABLE engineering_findings
    ADD COLUMN IF NOT EXISTS entity_type TEXT;
