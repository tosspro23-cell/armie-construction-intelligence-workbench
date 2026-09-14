-- SPEC-M11: grants the API's runtime role (0002a/0002b) access to
-- engineering_findings/engineering_finding_history (0006). Run against the
-- "armie" database, like 0002b/0004 -- a manual, out-of-band step, not
-- applied by CI or azure-deploy.yml.
--
-- Replace <API_IDENTITY_NAME> below with the same value used in 0002a/b/0004 --
-- e.g.:
--   sed -e "s/<API_IDENTITY_NAME>/$IDENTITY_NAME/g" \
--       apps/api/migrations/0007_grant_engineering_findings.sql > /tmp/0007.sql

-- No DELETE, matching the no-delete discipline every prior grant in this
-- project has followed (0002b/0004): findings and their history are
-- append-only/status-updated, never removed.
GRANT SELECT, INSERT, UPDATE ON engineering_findings TO "<API_IDENTITY_NAME>";
GRANT SELECT, INSERT ON engineering_finding_history TO "<API_IDENTITY_NAME>";
