-- SPEC-M4 addendum (independent-review fix): creates a NON-ADMIN, AAD-mapped
-- Postgres role for the API's runtime managed identity, instead of the
-- identity being the server's AAD administrator outright.
--
-- infra/bicep/data.bicep's Microsoft Entra administrator is now the
-- deploy/CI principal (deployPrincipalId), not the API's own identity. This
-- script is what actually gives the API identity a usable login, run ONCE
-- by whoever holds that admin access (the CI/CD OIDC principal, or an
-- owner via az login), after 0001_conversations_and_audit_events.sql and
-- after data.bicep has been deployed.
--
-- RUN THIS AGAINST THE "postgres" DATABASE, NOT "armie" (live-verified,
-- 2026-09-09, on a real Azure Database for PostgreSQL Flexible Server --
-- an earlier version of this script assumed it could run in one pass
-- against the application database; connecting to "armie" instead produces
-- `ERROR: function pgaadauth_create_principal_with_oid(...) does not
-- exist` -- the pgaadauth_* role-management functions are only exposed in
-- the "postgres" maintenance database, since role/login management is
-- cluster-wide, not per-database). Run 0002b_grant_api_runtime_role.sql
-- against "armie" afterward for the actual table grants.
--
-- Replace <API_IDENTITY_NAME> and <API_IDENTITY_OBJECT_ID> below with
-- platform.bicep's identityName / identityPrincipalId outputs before
-- running -- e.g.:
--   sed -e "s/<API_IDENTITY_NAME>/$IDENTITY_NAME/g" \
--       -e "s/<API_IDENTITY_OBJECT_ID>/$IDENTITY_PRINCIPAL_ID/g" \
--       apps/api/migrations/0002a_create_api_runtime_role.sql > /tmp/0002a.sql
-- then run /tmp/0002a.sql (see the invocation example at the bottom of this
-- file) rather than hand-editing this committed template in place.

SELECT * FROM pgaadauth_create_principal_with_oid(
    '<API_IDENTITY_NAME>',        -- becomes the Postgres role name the API connects as
    '<API_IDENTITY_OBJECT_ID>',   -- the managed identity's AAD object ID (principalId)
    'service',                    -- a managed identity/service principal, not a human user or group
    false,                        -- is_admin: explicitly NOT an administrator -- the entire point of this script
    false                         -- is_mfa: managed identity tokens don't carry interactive MFA claims
);

-- Example invocation (run as the AAD admin -- the deploy principal, per
-- data.bicep's deployPrincipalId -- e.g. after
-- `az account get-access-token --resource-type oss-rdbms` and using that
-- token as PGPASSWORD). Note dbname=postgres, not armie:
--
-- psql "host=<serverFqdn> dbname=postgres user=<deploy-principal-name> sslmode=require" \
--   -f /tmp/0002a.sql
