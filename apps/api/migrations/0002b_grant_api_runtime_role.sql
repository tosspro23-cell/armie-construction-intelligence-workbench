-- SPEC-M4 addendum: table-level grants for the role
-- 0002a_create_api_runtime_role.sql just created. Run this AGAINST THE
-- "armie" DATABASE (not "postgres" -- that's where 0002a runs), after
-- 0001_conversations_and_audit_events.sql and 0002a have both been applied.
--
-- Replace <API_IDENTITY_NAME> below with the same value used in 0002a --
-- e.g.:
--   sed -e "s/<API_IDENTITY_NAME>/$IDENTITY_NAME/g" \
--       apps/api/migrations/0002b_grant_api_runtime_role.sql > /tmp/0002b.sql

GRANT CONNECT ON DATABASE armie TO "<API_IDENTITY_NAME>";
GRANT USAGE ON SCHEMA public TO "<API_IDENTITY_NAME>";

-- Matches ConversationStore's interface exactly (get/set -- SELECT/UPSERT):
-- no DELETE, this role can never remove a conversation row.
GRANT SELECT, INSERT, UPDATE ON conversations TO "<API_IDENTITY_NAME>";

-- Matches AuditStore's append-only interface exactly (append/by_trace):
-- no UPDATE, no DELETE -- this role cannot alter or remove an audit event
-- once written, which is the actual security property this milestone's
-- independent review was asking for (a compromised API process should not
-- be able to tamper with its own audit trail).
GRANT SELECT, INSERT ON audit_events TO "<API_IDENTITY_NAME>";

-- Example invocation (run as the AAD admin, same as 0002a, but against the
-- "armie" database this time):
--
-- psql "host=<serverFqdn> dbname=armie user=<deploy-principal-name> sslmode=require" \
--   -f /tmp/0002b.sql
