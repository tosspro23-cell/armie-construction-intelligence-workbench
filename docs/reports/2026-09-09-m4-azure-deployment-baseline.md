# M4 — Azure Deployment Baseline (Postgres-backed persistence, live)

Owner-authorized run against the same Azure subscription as `docs/reports/2026-09-08-m3-azure-deployment-baseline.md`, 2026-09-09. Recorded per SPEC-M4's own acceptance criteria ("If deployed... a real follow-up chat request... retains conversation context across that restart") and this project's evidence-over-assertion pattern. This is the run that turns D-014 from "code and Bicep, not run against a real subscription" into an actually-verified claim.

## What was deployed

- `infra/bicep/data.bicep`: Azure Database for PostgreSQL Flexible Server (`armiem3-pg-33yetvtv5jbwa`, Burstable `Standard_B1ms`, 32GB storage, PostgreSQL 16), `passwordAuth: 'Disabled'` (AAD-only), the owner's own Azure CLI login as Microsoft Entra administrator (`deployPrincipalType='User'`), the API's managed identity (`armiem3-identity`) passed through as documentation only (not granted admin).
- `apps/api/migrations/0001_conversations_and_audit_events.sql`, `0002a_create_api_runtime_role.sql`, `0002b_grant_api_runtime_role.sql`: applied by hand against the live server (AAD admin token via `az account get-access-token --resource-type oss-rdbms`, `psycopg` — no `psql` client was available locally, so the same driver this project's stores use was used directly instead).
- `azure-deploy.yml` re-run (`workflow_dispatch`, `main`) with `database_url=postgresql://armiem3-identity@armiem3-pg-33yetvtv5jbwa.postgres.database.azure.com:5432/armie?sslmode=require`, rebuilding and redeploying `armiem3-api`/`armiem3-web` with `DATABASE_URL`/`DATABASE_USE_MANAGED_IDENTITY=true` added to the API container's env.

## Defects found only by deploying for real

Neither of these is reachable through the local `docker-compose` Postgres this milestone's code was otherwise verified against — both are specific to a real Azure Database for PostgreSQL Flexible Server.

1. **This Free Trial subscription is blocked from provisioning Postgres Flexible Server in `eastus2`** (and `eastus`, `westus2`, `southcentralus`, `westeurope`) — `az deployment group create` failed with a cryptic `ParameterOutOfRange: The value of the 'Version' should be in: []`, which traced back to `az postgres flexible-server list-skus --location eastus2` returning `"reason": "Subscriptions are restricted from provisioning in this region."` Resolved by checking several regions directly and deploying to `centralus` instead, which this subscription can provision in. The existing `eastus2` resources (Container Apps, ACR, OpenAI) were left in place; the Postgres server is cross-region from them, which only affects latency, not correctness (still a public-endpoint TLS connection either way).
2. **`pgaadauth_create_principal_with_oid` does not exist when connected to the application database** (`armie`) — only when connected to the `postgres` maintenance database, since Postgres role/login management is cluster-wide, not per-database. The migration script (previously a single unverified file) assumed one pass against `armie` would work; split into `0002a_create_api_runtime_role.sql` (run against `postgres`) and `0002b_grant_api_runtime_role.sql` (run against `armie`, the actual `GRANT` statements) after finding this live.

## End-to-end evidence

**Conversation persistence through the real deployed app, via `azure-deploy.yml`'s own smoke test:**

```
POST /api/v1/chat  {"thread_id": "smoke-test-1788957127", "question": "How many doors are on Level 01?"}
-> disposition: "answered"
-> answer: "The project contains **4** doors."

POST /api/v1/chat  {"thread_id": "smoke-test-1788957127", "question": "And how many on Level 02?"}
-> disposition: "answered"
-> answer: "Level 02 contains **2** doors."
-> subplan rationale: "Follow-up question referencing the previously counted
   entity (doors). User asks for the count limited to a named level
   (Level 02); map level to storey filter and run an IFC count on IfcDoor."
```

The model's own rationale text explicitly names this as a follow-up building on the prior turn — not just a disposition check, but the actual conversational-context mechanism (SPEC-M4's whole purpose) visibly firing, against the real deployed app, through the real `PostgresConversationStore`.

**Direct query against the live database, after the smoke test ran** (via `psycopg`, AAD admin token, no mock, no local docker Postgres involved):

```
SELECT thread_id, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 5;
-> smoke-test-1788957127 | 2026-09-09 12:32:28 UTC  (context includes the
   full previous_query_plan/previous_subplans from both turns above)
-> 6b2dec03-...           | 2026-09-09 12:31:33 UTC  (the deterministic
   smoke question's own thread)

SELECT count(*) FROM audit_events;
-> 77
```

This is the literal "visible trace in the cloud" this milestone's acceptance criteria asked for: real rows, written by the real deployed Container App, queryable independently of the app itself.

**Least-privilege grant confirmed working end-to-end, not just applied**: the deployed API container successfully wrote to `conversations` and `audit_events` using its own managed-identity-derived Postgres role (`armiem3-identity`, created by `0002a`, granted only `SELECT/INSERT/UPDATE` on `conversations` and `SELECT/INSERT` on `audit_events` by `0002b`) — the smoke test's success is itself proof this role's exact, minimal grant set is sufficient for the app's real read/write pattern, not just a theoretical policy.

## Corrections to prior claims

D-014 (`docs/decisions/README.md`) previously stated the Bicep templates were "validated with `az bicep build`... not run against a real Azure subscription this session" and that `0002_grant_api_runtime_role.sql`'s syntax was "NOT verified against a live server." Both are now out of date as of this deployment; D-014 is corrected in place (flagged, not silently edited) rather than left to imply the gap still exists.

## Cost posture

Burstable `Standard_B1ms`, 32GB storage — the exact configuration Azure's free-account 12-months-free allowance covers (verified against this subscription's own `quotaId: FreeTrial_2014-09-01` and the owner's confirmation via the Portal's Free Services page before deploying). `centralus` was chosen because of the region restriction above, not for cost reasons. No other resource tier changed from the M3 baseline.
