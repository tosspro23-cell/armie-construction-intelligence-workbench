-- SPEC-M9 addendum: grants the API's runtime role (0002a/0002b) access to
-- thread_projects (0003) -- found live against the real deployment, not
-- assumed: 0003 was applied without a matching grant, and the very first
-- real chat request afterward failed with "permission denied for table
-- thread_projects". Run against the "armie" database, like 0002b.
--
-- Replace <API_IDENTITY_NAME> below with the same value used in 0002a/b --
-- e.g.:
--   sed -e "s/<API_IDENTITY_NAME>/$IDENTITY_NAME/g" \
--       apps/api/migrations/0004_grant_thread_projects.sql > /tmp/0004.sql

-- Matches ConversationStore.bind_project's own interface exactly (one
-- claim-or-read INSERT ... ON CONFLICT DO UPDATE ... RETURNING): no
-- DELETE, the same no-delete discipline 0002b already established for
-- conversations/audit_events.
GRANT SELECT, INSERT, UPDATE ON thread_projects TO "<API_IDENTITY_NAME>";
