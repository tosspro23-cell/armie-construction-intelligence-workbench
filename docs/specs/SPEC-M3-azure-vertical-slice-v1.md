# SPEC-M3: Azure Vertical Slice — Phase 1 of the Azure Cloud-Native Roadmap — v1

## 1. Objective

Deploy the existing system to Azure as a genuinely working, minimal vertical slice —
one Container Apps environment running the existing API and web containers, one
real chat request answered end-to-end through a new Azure OpenAI / Managed Identity
provider path, with a basic Application Insights trace as evidence — without
altering the deterministic-first architecture, the disposition taxonomy, or any
existing evidence/verification invariant.

This is Phase 1 of a five-phase Azure roadmap agreed with the owner on 2026-09-08
(independent architecture review, this repository's conversation history).
Phases 2–5 (production hardening, enterprise retrieval, AKS/platform engineering,
construction-intelligence expansion) are explicitly out of scope for this spec and
will each get their own spec document before implementation.

## 2. Rationale

The owner is preparing for an Azure-heavy AI engineering interview and wants to
deepen this existing flagship project with real cloud/production evidence rather
than build a second, unrelated project. The review that preceded this spec
concluded that the highest-value, lowest-risk first step is a real, working
deployment — not a broad simultaneous rollout of every Azure service named in the
target job description. A thin, real vertical slice is stronger interview evidence
than a wide, undeployed design, and it surfaces genuine blocking gaps (found below)
that a documents-only exercise would not have caught.

The review also concluded this project must stay differentiated from the owner's
existing healthcare Azure project (Functions/Durable Functions/Cosmos DB): this
milestone deliberately uses containerized compute (Container Apps now, AKS in
Phase 4) and, when persistence is introduced in a later phase, PostgreSQL rather
than Cosmos DB (OD-20).

## 3. Verified current-state assumptions

Checked directly against this checkout on branch `feat/m3-azure-vertical-slice`
(base `main` @ `c1c6d72`), not assumed:

- `apps/api/app/providers/` contains only `ollama_provider.py` and
  `openai_provider.py`; there is no Azure Identity or Managed Identity code path
  anywhere in `apps/api`. `apps/api/pyproject.toml` has no `azure-identity` or
  OpenTelemetry dependency.
- `apps/api/app/config.py` has no Azure-related settings field. `Settings.openai_api_key`
  is the only credential-shaped field that exists today, used for the direct OpenAI
  path — this milestone does not touch it.
- `apps/api/app/config.py:36` declares `checkpoint_db_path` (also present in
  `.env.example`); a repository-wide search of `apps/api/app` finds no second
  reference to it anywhere. It is dead configuration — no LangGraph checkpointer is
  actually wired to it. This is a pre-existing finding, not something this
  milestone introduces or needs; see §9 for its disposition.
- `apps/api/app/main.py:39-44` hardcodes `allow_origins` to three localhost
  origins. `apps/api/app/main.py:47` already exposes `/api/v1/health`, suitable
  as-is for a container health probe.
- `apps/api/app/main.py`'s `app.state.conversations` and `app.state.requests`
  (lines 29-30) are plain in-process Python dicts. There is no shared/external
  state store. Running more than one replica of this container would silently
  fragment conversation resume and cancellation tracking across replicas, with no
  error raised — this is a real constraint on Phase 1's deployment topology, not a
  hypothetical one (see OD-22).
- `apps/web/vite.config.ts:10` proxies `/api` to `API_PROXY_TARGET` in local dev
  only. `apps/web/src/main.tsx` calls only relative paths (`/api/v1/...`) — verified
  by direct inspection, not assumed. `apps/web/Dockerfile` builds an `nginx:1.27-alpine`
  image serving the static `dist/` output with no reverse-proxy configuration of any
  kind. Deployed as-is behind a separate origin from the API, every API call from
  the browser would 404. This is a genuine blocking gap for a two-container
  deployment, found by direct inspection during this milestone's pre-spec scan.
- `apps/web/Dockerfile:3` runs `npm install`, not `npm ci` — inconsistent with this
  repository's own reproducible-build invariant (D-006, which fixed the equivalent
  problem for local development). The frontend container image's build is not
  currently guaranteed reproducible.
- `.github/workflows/ci.yml` runs backend tests, frontend build, and `ruff` lint
  only. There is no deploy workflow, no Bicep/Terraform, and no Azure Container
  Registry reference anywhere in the repository.
- `docker --version` succeeds in the current environment (Docker 29.7.2), so local
  Docker build verification is possible during implementation; it was not run as
  part of this spec (spec-first — no code or build verification before the spec
  itself is committed).

## 4. Allowed scope

**A. New Azure OpenAI provider.** `apps/api/app/providers/azure_openai_provider.py`
implementing the existing `ModelProvider` protocol (`apps/api/app/providers/base.py`),
using `azure-identity`'s `DefaultAzureCredential` (or `ManagedIdentityCredential` in
the deployed environment) via `openai`'s `get_bearer_token_provider` against the
Cognitive Services scope, and `openai.AzureOpenAI`. No API-key code path is added
for Azure — Managed Identity only (OD-23).

**B. Factory wiring.** `apps/api/app/providers/factory.py` gains an `llm_provider
== "azure"` branch, following the exact same centralized-selection discipline D-007
already established. `ollama`, `openai`, and `hybrid` selection logic is untouched.

**C. Configuration surface.** `apps/api/app/config.py` and `.env.example` gain:
`azure_openai_endpoint`, `azure_openai_api_version`, `azure_openai_text_deployment`,
`azure_openai_vision_deployment`, `otel_exporter_connection_string` (empty by
default — matches the existing opt-in pattern used for `ollama_escalation_model`;
an empty value means OpenTelemetry exports nowhere, not that it's silently
required).

**D. Minimal OpenTelemetry wiring.** FastAPI and `httpx` auto-instrumentation in
`apps/api/app/main.py`/`services.py`, exporting to Application Insights only when
`otel_exporter_connection_string` is set. This is additive transport only — it
must not change `/api/v1/traces/{trace_id}`, `AuditStore`, or any existing audit
event shape. The richer AI-specific telemetry model (per-stage latency, disposition
distribution, token/cost attribution) from the broader review is explicitly
deferred to Phase 2 (§5).

**E. Frontend reverse proxy, not a CORS change.** `apps/web/Dockerfile` gains an
nginx runtime config (`default.conf.template` + the standard `nginx:alpine`
`envsubst`-on-start pattern) that reverse-proxies `/api/*` to the backend Container
App's address, configurable via an environment variable at container start. No
frontend source code changes, no CORS policy change in `apps/api/app/main.py` —
the reverse-proxy design is chosen specifically so this invariant does not need to
move. `apps/web/Dockerfile:3`'s `npm install` is corrected to `npm ci` in the same
change, since it sits on the same line this milestone must touch anyway to make
the image build reproducibly.

**F. Infrastructure as code.** New `infra/bicep/` directory: one Container Apps
Environment, two Container Apps (`api`, `web`) each pinned to
`minReplicas: 1, maxReplicas: 1` (OD-22), one user-assigned managed identity
granted the `Cognitive Services OpenAI User` role on the target Azure OpenAI
resource, a Log Analytics workspace and Application Insights resource wired to the
OTel exporter connection string, and an Azure Container Registry.

**G. Deploy workflow.** `.github/workflows/azure-deploy.yml`, triggered only by
`workflow_dispatch` (manual) — never on every push to `main` — building and pushing
both images to ACR and applying the Bicep templates.

**H. Dependencies.** `apps/api/pyproject.toml` gains `azure-identity` and the
OpenTelemetry packages needed for (D), as base (not `dev`-only) dependencies.

## 5. Explicitly excluded scope

- AKS, Azure AI Search, ADLS Gen2, PostgreSQL/Cosmos DB, Key Vault (Managed
  Identity alone covers this milestone's only credential-shaped need).
- Multi-replica scaling or any shared/external state store — `app.state.conversations`
  and `app.state.requests` remain in-process for this phase (OD-22).
- The richer AI-specific telemetry model (disposition-rate metrics, token/cost
  attribution, per-stage latency breakdown) — transport only in this phase.
- Any change to `apps/api/app/agent/graph.py`'s orchestration logic, the
  disposition taxonomy (D-010), the evidence/verification contracts (D-001/D-003),
  or the cross-source-reconciliation carve-out (D-011).
- Any change to CORS policy.
- Terraform, or Azure DevOps as a primary pipeline (OD-21).
- Wiring up or removing the dead `checkpoint_db_path` setting (§3) — recorded in
  `docs/decisions/REVIEW_REQUIRED.md` per §9, not fixed here.
- Any claim in `PROJECT_STATE.md`/`README.md` that Azure deployment is validated
  until a real deployment has actually been run and evidenced (§8).

## 6. Affected surfaces

`apps/api/app/providers/azure_openai_provider.py` (new),
`apps/api/app/providers/factory.py`, `apps/api/app/config.py`, `.env.example`,
`apps/api/app/main.py` (OTel instrumentation only), `apps/api/app/services.py`
(OTel instrumentation only), `apps/api/pyproject.toml`, `apps/web/Dockerfile`,
new `apps/web/default.conf.template` (nginx), new `infra/bicep/*.bicep`, new
`.github/workflows/azure-deploy.yml`, plus a new test module for
`AzureOpenAIProvider` and any existing test fixtures needed to mock Azure
credentials/token acquisition.

## 7. Invariants

- D-001 (deterministic facts remain authoritative), D-002 (typed plan + capability
  gate), D-003 (evidence precedes verification), D-004/D-010 (honest disposition
  taxonomy), and D-011 (narrow reconciliation carve-out) are all unchanged by this
  milestone — this is an infrastructure/provider-portability milestone, not a
  reasoning-path change.
- Provider selection logic stays centralized in `providers/factory.py` (D-007's
  discipline) — the new `azure` branch must not duplicate selection logic
  elsewhere.
- No API key or connection string may be embedded in a container image or Bicep
  template in plaintext. Managed Identity is the only Azure auth path this
  milestone adds.
- All 167 existing tests remain green and behaviorally unchanged; the new provider
  path is additive and tested via a mocked credential/token provider and a mocked
  Azure OpenAI response — zero live Azure calls in CI, mirroring `FakeModelProvider`'s
  no-network discipline (D-007).
- `apps/api/app/main.py`'s CORS policy is unchanged.

## 8. Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` and
  `(cd apps/web && npm ci && npm run build)` both still pass with no flags.
- `ruff check --select F,E9,I,F401 apps/api tests` passes.
- New tests for `AzureOpenAIProvider` cover: successful structured/vision-structured
  calls against a mocked Azure OpenAI response, and a token-acquisition failure
  mapping to the existing `error` disposition path — zero live Azure calls.
- `docker build` succeeds locally for both `apps/api/Dockerfile` and
  `apps/web/Dockerfile` (the web build specifically re-verified with `npm ci`, no
  flags, no `--legacy-peer-deps`).
- A real, owner-executed deployment (not something CI can assert) produces: one
  Container Apps environment with both apps running, the web app reachable over
  HTTPS, and one real end-to-end chat request answered through the `azure`
  provider profile with a visible corresponding trace in Application Insights.
  This spec's job is to make that deployment reproducible via Bicep and the deploy
  workflow — not to claim, in this document, that it has already happened.
- No release-claim language changes in `PROJECT_STATE.md`/`README.md` until that
  real deployment has been run once and evidenced.

## 9. Documentation / write-back requirements

- `PROJECT_STATE.md`: new milestone entry for "M3 Phase 1 — Azure Vertical Slice"
  once merged, in the same honest style as the M1/M1.5/M2 entries (what was
  proven with evidence, what was deliberately deferred).
- New ADR `D-012`: provider portability now includes an Azure OpenAI / Managed
  Identity path; Container Apps chosen over AKS for Phase 1 with the concrete
  reason (OD-19).
- New `docs/decisions/REVIEW_REQUIRED.md` entry for the pre-existing dead
  `checkpoint_db_path` setting found during this milestone's scan (found, not
  fixed — same treatment this file already gives comparable findings).
- `AGENT_HANDOFF.md` code map gets one new row each for
  `apps/api/app/providers/azure_openai_provider.py` and `infra/bicep/`.
- A `docs/reports/2026-MM-DD-m3-azure-deployment-baseline.md` note (mirroring the
  existing `docs/reports/2026-08-24-m2p1-live-model-baseline.md` pattern) recording
  the actual first real deployment run: what was deployed, one real question asked
  and answered, the Application Insights trace reference, and the measured cost for
  that session.

## 10. Git, stop conditions

Branch `feat/m3-azure-vertical-slice`. This spec is committed alone, before any
code or infrastructure changes. One commit per Allowed Scope subsection: (A+B+C+H)
provider + config + deps, (D) observability wiring, (E) frontend proxy + Dockerfile
fix, (F) Bicep infra, (G) deploy workflow. No squash; owner-authorized merge only,
per this repository's PR/merge-is-owner-only rule.

**Stop and report, do not proceed or improvise, if:**
- The frontend's relative `/api/*` call pattern turns out to depend on anything
  beyond plain HTTP request/response once implementation starts (e.g., a
  WebSocket/SSE upgrade path needing extra nginx directives) that a plain reverse
  proxy config doesn't already cover.
- Managed-Identity-based Azure OpenAI authentication cannot actually obtain a token
  in the target subscription's configuration (tenant/RBAC propagation issue) — do
  not silently fall back to an API-key path; that would violate this milestone's
  own invariant (§7). Report and let the owner decide per OD-23.
- Correcting `npm install` → `npm ci` in `apps/web/Dockerfile` surfaces a real
  dependency resolution failure — that would mean this repository's own D-006
  reproducible-install claim does not actually hold for the container image build
  path specifically. Report; do not paper over with `--legacy-peer-deps`.

## 11. Owner decisions

- **OD-19 — Phase 1 compute target. RESOLVED (owner conversation, 2026-09-08).**
  Azure Container Apps is the only compute target for Phase 1. AKS is deferred to
  Phase 4 and must be gated on a concrete capability Container Apps cannot
  provide (a KEDA/queue-triggered worker for batch IFC/PDF processing jobs), not a
  duplicate deployment of the same web application.
- **OD-20 — primary operational datastore. RESOLVED (owner conversation,
  2026-09-08).** PostgreSQL (Azure Database for PostgreSQL Flexible Server), not
  Cosmos DB, will be the platform's primary store once persistence is introduced
  in Phase 2. Out of scope for this Phase 1 spec, which introduces no new
  persistent state beyond what already exists.
- **OD-21 — IaC and CI/CD tooling. RESOLVED (owner conversation, 2026-09-08).**
  Bicep is the primary IaC tool; GitHub Actions is the primary CI/CD. A
  lightweight, non-primary Azure Pipelines YAML artifact may be added in a later
  phase purely as breadth evidence for the target job description; Terraform is
  not planned.
- **OD-22 — Phase 1 replica count. OPEN — needs explicit owner sign-off before
  infrastructure is applied.** Recommend pinning Container Apps `minReplicas:
  maxReplicas: 1` for Phase 1, because `apps/api/app/main.py`'s conversation and
  request-tracking state (`app.state.conversations`, `app.state.requests`) is
  in-process memory and would silently fragment across replicas with no error.
  This should be documented as a known limitation (matching `PROJECT_STATE.md`'s
  existing "Known limitations" style) rather than left implicit, since it
  directly bounds any "production-grade" claim about this phase.
- **OD-23 — Azure auth fallback policy. OPEN — needs explicit owner sign-off.**
  This milestone adds only a Managed-Identity auth path for Azure OpenAI, no
  API-key fallback. If the target Azure subscription cannot grant the Container
  App's managed identity the `Cognitive Services OpenAI User` role in time (org
  policy, RBAC propagation delay), should implementation (a) stop and report, or
  (b) accept a temporary, explicitly-flagged Key-Vault-brokered API-key exception?
  Recommend (a). Flagging in advance rather than deciding mid-implementation.
