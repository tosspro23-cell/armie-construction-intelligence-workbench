# M3 Phase 1 — Azure Deployment Baseline

Owner-executed run against a real, newly-created Azure subscription, 2026-09-08.
Recorded per `docs/specs/SPEC-M3-azure-vertical-slice-v1.md` §9, mirroring
`docs/reports/2026-08-24-m2p1-live-model-baseline.md`'s pattern: this is the
evidence that the vertical slice actually works, not a claim without it.

## What was deployed

- Resource group `armie-m3-rg` (`eastus2`), fresh Azure subscription.
- `infra/bicep/platform.bicep`: Log Analytics workspace (`armiem3-logs`),
  workspace-based Application Insights (`armiem3-insights`), Azure Container
  Registry (`armiem3acr33yetvtv5jbwa`, no admin user), a user-assigned managed
  identity (`armiem3-identity`), and its `AcrPull` + `Cognitive Services OpenAI
  User` role assignments.
- An Azure OpenAI account (`armie-m3-openai`) created out-of-band via
  `az cognitiveservices account create` (per `platform.bicep`'s documented
  Phase 1 simplifying default — not Bicep-managed), with one `gpt-5-mini`
  (`2025-08-07`, `GlobalStandard`, capacity 10) deployment, also named
  `gpt-5-mini`.
- `infra/bicep/apps.bicep`: one Container Apps Environment (`armiem3-env`),
  the API app (`armiem3-api`, internal-only ingress) and the web app
  (`armiem3-web`, external ingress), both pinned to `minReplicas: maxReplicas:
  1` (OD-22).
- Both images built locally for `linux/amd64` (this machine is Apple Silicon;
  Azure Container Apps does not run `arm64`) and pushed to the registry —
  `az acr build` (ACR Tasks) was attempted first and rejected
  (`TasksOperationsNotAllowed`, a default anti-abuse restriction on new
  subscriptions), so `docker buildx build --platform linux/amd64 --push` was
  used instead.

## Defects found only by deploying for real

None of the following is reachable through `FakeModelProvider` or any other
existing test double — each was found by actually running the built images
against a real Azure subscription and a real Azure OpenAI resource. All are
fixed and committed on `feat/m3-azure-vertical-slice`; full detail in D-012
(`docs/decisions/README.md`) and each fix's own commit message.

1. `apps/api/app/config.py`'s `.env` path computation crashed the API
   container at import time (wrong directory-depth assumption).
2. `infra/bicep/platform.bicep` had the wrong GUID for the built-in `AcrPull`
   role.
3. `AzureOpenAIProvider` needed `aiohttp` (an undeclared transitive
   dependency of `azure-identity.aio`'s async credential).
4. Both `OpenAIProvider` and `AzureOpenAIProvider` used the Responses API,
   which 404s on this Azure OpenAI resource; switched to Chat Completions.
5. Chat Completions' `strict: true` JSON-schema mode rejected `QueryPlan`'s
   free-form `filters` dict field; dropped `strict` mode entirely rather than
   restructure a protected contract.
6. The internal-only API app's URL needed `https://`, not `http://`
   (Container Apps' ingress edge terminates TLS for internal apps too).
7. nginx's reverse proxy needed `proxy_ssl_server_name on` (SNI) and
   `proxy_set_header Host $proxy_host` (not the web app's own `$host`) to
   route to the internal API app.
8. `apps/api/Dockerfile` never bundled `demo_data/`, so the deployed API had
   nothing to answer questions about until fixed.

## End-to-end evidence

**Deterministic path (zero model calls), verified live:**

```
POST /api/v1/chat  {"question": "How many doors are in this project?"}
-> disposition: "answered"
-> answer: "The project contains **4** doors."
-> 4 citations (real IFC GlobalId/ExpressID locators)
-> verification.status: "passed"
-> model_call_count: 0, tool_call_count: 1
```

**Semantic path through real Azure OpenAI / Managed Identity, verified live:**

```
POST /api/v1/chat  {"question": "Please describe the situation with the
                     doors in this project in your own words."}
-> disposition: "answered"
-> answer: "Per-storey door counts: Level 01: 2; Level 02: 2."
-> 4 citations
-> verification.status: "passed"
-> model_call_count: 2, tool_call_count: 1
-> configured_provider: "azure", actual_model: "gpt-5-mini"
```

Both requests were made against the public web app FQDN
(`https://armiem3-web.thankfulcliff-c5f74fed.eastus2.azurecontainerapps.io`),
proxied by nginx to the internal-only API app — the API itself is not
publicly reachable (confirmed: a direct request to the API app's own FQDN
returns Azure's platform 404).

**Application Insights trace (workspace-based; queried via the Log Analytics
workspace's `AppDependencies` table, not the classic `requests`/`dependencies`
names):**

```
AppDependencies | order by TimeGenerated desc | take 10
```

shows, in order, the failed `POST /openai/responses` calls from before the
Chat Completions fix (`Success: False`) and the successful
`POST /openai/deployments/gpt-5-mini/chat/completions` calls after it
(`Success: True`, durations 2.3s–15.5s), against `Target:
armie-m3-openai.openai.azure.com` — a real, queryable trace of the exact
debugging journey and the working final state.

**Known follow-up, not blocking:** `AppRequests` (FastAPI's own server-span
telemetry) shows zero rows despite `AppDependencies`/`AppMetrics` receiving
data, meaning `FastAPIInstrumentor.instrument_app(app)` is not producing
exported server spans for some reason not yet diagnosed. The OTel export
pipeline itself is proven working (dependency telemetry above), so this is a
narrower, separate gap — deferred to a later phase rather than investigated
further here, since the milestone's evidence goal (a real, working Azure
OpenAI call with a visible corresponding Application Insights trace) is
already met via the dependency telemetry.

**Also observed, not blocking:** each provider call constructs a new
`AsyncAzureOpenAI` client and a new `DefaultAzureCredential` rather than
reusing one, producing a benign `"Event loop is closed"` warning in container
logs during async client cleanup. No request failed because of it, but
reusing a single client/credential per process is a reasonable follow-up for
Phase 2.

## Cost posture

`gpt-5-mini` at `GlobalStandard` capacity 10 (10K TPM), both Container Apps
pinned to exactly one replica, `Basic` ACR, `PerGB2018` Log Analytics — all
the lowest-cost tier for each service, consistent with the review's
cost-control guidance. No resource in this deployment auto-scales up.
