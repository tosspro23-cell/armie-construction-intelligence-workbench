-- SPEC-M4 addendum (independent-review fix): grants the API's runtime
-- managed identity a NON-ADMIN, table-scoped Postgres role, instead of the
-- identity being the server's AAD administrator outright.
--
-- infra/bicep/data.bicep's Microsoft Entra administrator is now the
-- deploy/CI principal (deployPrincipalId), not the API's own identity.
-- This script is what actually gives the API identity a usable login, run
-- ONCE by whoever holds that admin access (the CI/CD OIDC principal, or an
-- owner via az login), after 0001_conversations_and_audit_events.sql and
-- after data.bicep has been deployed.
--
-- pgaadauth_create_principal_with_oid is an Azure Database for PostgreSQL
-- Flexible Server extension function, not a plain Postgres one -- it does
-- not exist on the local docker-compose Postgres this project's other
-- migration/tests run against, so unlike 0001, this script's exact syntax
-- is NOT verified against a live server this session (no Azure Postgres
-- Flexible Server was deployed). Verify the function name/argument order
-- against the current Microsoft documentation before running this for
-- real: https://learn.microsoft.com/azure/postgresql/flexible-server/how-to-manage-azure-ad-users
--
-- Replace <API_IDENTITY_NAME> and <API_IDENTITY_OBJECT_ID> below (every
-- occurrence) with platform.bicep's identityName / identityPrincipalId
-- outputs before running -- e.g.:
--   sed -e "s/<API_IDENTITY_NAME>/$IDENTITY_NAME/g" \
--       -e "s/<API_IDENTITY_OBJECT_ID>/$IDENTITY_PRINCIPAL_ID/g" \
--       apps/api/migrations/0002_grant_api_runtime_role.sql > /tmp/0002.sql
-- then run /tmp/0002.sql (see the invocation example at the bottom of this
-- file) rather than hand-editing this committed template in place.

SELECT * FROM pgaadauth_create_principal_with_oid(
    '<API_IDENTITY_NAME>',        -- becomes the Postgres role name the API connects as
    '<API_IDENTITY_OBJECT_ID>',   -- the managed identity's AAD object ID (principalId)
    'service',                    -- a managed identity/service principal, not a human user or group
    false,                        -- is_admin: explicitly NOT an administrator -- the entire point of this script
    false                         -- is_mfa: managed identity tokens don't carry interactive MFA claims
);

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

-- Example invocation (run as the AAD admin -- the deploy principal, per
-- data.bicep's deployPrincipalId -- e.g. after
-- `az account get-access-token --resource-type oss-rdbms` and using that
-- token as PGPASSWORD):
--
-- psql "host=<serverFqdn> dbname=armie user=<deploy-principal-name> sslmode=require" \
--   -f /tmp/0002.sql
