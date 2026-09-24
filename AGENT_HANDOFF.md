# Agent Handoff

This is a cold-start guide for a new vendor or frontier coding agent. Read this file together with [`PROJECT_STATE.md`](PROJECT_STATE.md), [`README.md`](README.md), [`docs/architecture.md`](docs/architecture.md), [`SECURITY_AND_DATA.md`](SECURITY_AND_DATA.md), [`docs/decisions/README.md`](docs/decisions/README.md), and [`docs/decisions/REVIEW_REQUIRED.md`](docs/decisions/REVIEW_REQUIRED.md) before editing code.

## Start here

1. Confirm the checkout and branch are clean: `git status --short --branch`.
2. Run the two authoritative local checks:

   ```bash
   PYTHONPATH=apps/api python3 -m pytest -q
   (cd apps/web && npm ci && npm run build)
   ```

   Both must pass with no flags; `.github/workflows/ci.yml` runs the same checks (backend on
   Python 3.9-3.12, frontend on Node 22, plus `ruff`) on every push and pull request.

3. Start Ollama with the models in `.env.example`, start FastAPI, then start Vite and run one synthetic browser query. Do not use private assignment files to diagnose the public repository.
4. Read the relevant code path before making a proposal. Do not infer production readiness from screenshots or from a passing compile alone.

## Critical code map

| Concern | Files |
|---|---|
| HTTP lifecycle, CORS, chat/cancel/trace endpoints | `apps/api/app/main.py` |
| Settings, fixture paths, provider/model/timeouts | `apps/api/app/config.py` |
| LangGraph state, routing, execution, synthesis, audit | `apps/api/app/agent/graph.py` |
| Fast-path semantics and source/intent detection | `apps/api/app/agent/router.py` |
| Plan canonicalization and cross-field validation | `apps/api/app/agent/plan_validation.py` |
| Typed request/plan/response/evidence contracts | `apps/api/app/schemas/models.py`, `apps/api/app/schemas/vision.py` |
| Ollama/OpenAI/Azure OpenAI provider boundary; centralized provider selection | `apps/api/app/providers/`, `apps/api/app/providers/factory.py` |
| Azure OpenAI provider, Managed Identity only -- no API-key path (SPEC-M3, D-012) | `apps/api/app/providers/azure_openai_provider.py` |
| Provider **factory** injection seam (text/vision/escalation) + conversation/audit store factory seam, tool services | `apps/api/app/services.py` (`ServiceContainer`) |
| OpenTelemetry -> Application Insights wiring, opt-in and inert by default (SPEC-M3) | `apps/api/app/telemetry.py` |
| Shared-secret auth for the public API, app-wide FastAPI dependency, no-op unless set (SPEC-M5); per-caller request-ownership check via a server-issued session token (D-016) | `apps/api/app/security.py` |
| Frontend auth-header/key-gate/blob-fetch helpers shared with `IfcViewer.tsx` (SPEC-M5) | `apps/web/src/apiClient.tsx` |
| `ConversationStore`/`AuditStore` interfaces + in-memory/JSONL (default) and Postgres (opt-in, `DATABASE_URL`) implementations (SPEC-M4, D-014) | `apps/api/app/persistence/` |
| Postgres schema for the two tables above (applied by hand, no migration framework) | `apps/api/migrations/0001_conversations_and_audit_events.sql` |
| IFC deterministic adapter | `apps/api/app/tools/ifc/repository.py` |
| PDF rendering/native lookup/vision preparation | `apps/api/app/tools/document/analyzer.py` |
| Multi-document corpus support: one `DocumentAnalyzer` per `Settings.pdf_files` entry, naive zero-model-call multi-document lookup (SPEC-M6, D-017) | `apps/api/app/services.py` (`document_analyzers`), `apps/api/app/agent/graph.py` (`_execute_pdf_multi_document`) |
| Azure AI Search retrieval fallback (opt-in, `AZURE_SEARCH_ENDPOINT`) -- ranks documents by relevance, informs the directed-vs-blanket miss message, never a substitute for `native_lookup` (SPEC-M7, D-018) | `apps/api/app/retrieval.py`, `apps/api/app/providers/azure_openai_provider.py` (`AzureOpenAIEmbeddingProvider`), `apps/api/app/agent/graph.py` (`_retrieve_relevant_documents`), `scripts/index_document_corpus.py` |
| Per-caller rate limiting on `/api/v1/chat`/resume (opt-in, `RATE_LIMIT_REQUESTS_PER_MINUTE`), in-process only (SPEC-M6's OD-22 single-replica pin) (D-019) | `apps/api/app/rate_limit.py`, `apps/api/app/security.py` (`require_rate_limit`) |
| Evidence crop persistence via Azure Blob Storage (opt-in, `EVIDENCE_STORAGE_ACCOUNT_URL`) -- dual-write from `DocumentAnalyzer.crop_evidence`, blob-first/local-fallback serving (SPEC-M8, D-020) | `apps/api/app/evidence_storage.py`, `apps/api/app/tools/document/analyzer.py` (`crop_evidence`), `apps/api/app/main.py` (`evidence_file`) |
| Multi-project workspace (opt-in, `ADLS_ACCOUNT_URL`) -- `ServiceContainer.get_project`, per-project `ProjectResources`, one thread bound to one project for its lifetime (SPEC-M9, D-023) | `apps/api/app/services.py` (`ServiceContainer.get_project`, `ProjectResources`), `apps/api/app/persistence/*` (`bind_project`), `demo_data/projects_registry.json` |
| Optional answer-wording polish (opt-in, `ENABLE_ANSWER_POLISH`) -- number-preservation guard is the actual enforcement, not the prompt (SPEC-M10, D-025) | `apps/api/app/agent/graph.py` (`_polish_answer`, `_polish_preserves_facts`) |
| Cloud Provenance -> Application Insights deep link (opt-in, `AZURE_TENANT_ID`/`APP_INSIGHTS_RESOURCE_ID`) -- tags the OpenTelemetry span with `AgentService.invoke`'s own `trace_id`, not the request-lifecycle `request_id` (D-028, fixed in D-031 after shipping with the wrong ID) | `apps/api/app/main.py` (`_tag_span_with_trace_id`), `apps/api/app/agent/graph.py` (`_cloud_trace_url`, `_cloud_trace_query`) |
| Independent and invariant verification | `apps/api/app/verification/verifiers.py` |
| **V2 tool-calling agent** -- a bounded iteration loop (`invoke_v2`) where each iteration either dispatches model-requested tool calls or produces a final answer; narrative-consistency verification cross-checks the final answer's own numbers against this turn's real tool results (SPEC-M16, D-061/D-062, plus six real false-positive fixes D-065/D-066/D-068/D-069/D-073) | `apps/api/app/agent/graph.py` (`invoke_v2`, `_v2_dispatch_tool`, `_narrative_consistent_with_tool_facts`), `apps/api/app/agent/tools.py` (tool schemas) |
| **Engineering Finding workflow** -- promotes a non-matched reconciliation item into a persisted, human-reviewable object with an enforced state machine (open → acknowledged → action required → resolved → re-verified/closed); the V2 agent can investigate one and submit a typed verdict via `submit_finding_verdict`, never writing to the real IFC/PDF sources itself (SPEC-M11, SPEC-M17, D-063/D-064) | `apps/api/app/finding_workflow.py` (state machine), `apps/api/app/persistence/finding_store.py` (`InMemoryFindingStore`/`PostgresFindingStore`), `apps/api/app/agent/graph.py` (`build_finding_investigation_question`, `build_finding_proposal`, `_upsert_findings_from_reconciliation`), `apps/web/src/Findings.tsx` |
| Decision Trace panel -- linear Question/Plan/Execution/Evidence/Verification/Result trace with per-step raw audit events, real dispatched tool calls/arguments, and cloud provenance one click away | `apps/web/src/DecisionStory.tsx` |
| Browser state, viewer, citations, audit grouping, cancellation | `apps/web/src/main.tsx`, `apps/web/src/IfcViewer.tsx`, `apps/web/src/styles.css` |
| Deterministic-contract, failure-path, characterization, and seam-invariance tests | `tests/` (see `docs/specs/SPEC-M1-reliability-foundation-v1.md`) |
| The only fake used to drive the probabilistic path in tests | `tests/fakes/fake_provider.py` (`FakeModelProvider`) |
| Phase 1 Azure infrastructure: registry/identity/monitoring, then Container Apps (SPEC-M3) | `infra/bicep/platform.bicep`, `infra/bicep/apps.bicep` |
| Postgres data tier (SPEC-M4) -- not deployed automatically, see the file header before running | `infra/bicep/data.bicep` |
| Manual-only (`workflow_dispatch`) build-and-deploy pipeline (SPEC-M3) | `.github/workflows/azure-deploy.yml` |

## End-to-end lifecycle

`POST /api/v1/chat` creates a request-scoped invocation. LangGraph resolves viewer/conversation context, selects a deterministic or semantic plan, canonicalizes and validates it, checks capability, executes a source-specific tool, creates evidence, verifies the result, and returns an `AgentResponse`. The API commits context updates only for terminal answered/clarification flows. `/api/v1/traces/{trace_id}` exposes the audit events used by the UI. `/api/v1/requests/{request_id}/cancel` is the cancellation boundary; both it and `GET /api/v1/requests/{request_id}` require the caller's `X-Session-Id` to match the session that created the request (D-016), unless the request was created without one.

## Contracts that must remain stable

- `QueryPlan` is the canonical single-task contract; `MultiQueryPlan` is the bounded decomposition contract.
- `IfcQueryInput`/`IfcQueryResult` isolate deterministic IFC operations and evidence.
- `Evidence` and `Citation` carry source type plus a locator (IFC GlobalId/ExpressID, or PDF page/bbox/field).
- `VerificationStatus` contains one or more `VerifierResult` records; an answer without sufficient evidence should not be presented as verified.
- `AgentResponse` is the browser/API boundary: disposition, answer, citations, verification, execution metadata, and context update.
- `ViewerContext` carries selected IDs, optional snapshot bytes, camera metadata, and explicit clear-state flags.
- `ModelProvider` exposes typed text and vision calls (`name: str`, `model: str`, `structured`, `vision_structured`). Models may interpret; they must not become the authority for IFC arithmetic. `ServiceContainer` injects provider *factories* (`text_provider_factory`, `vision_provider_factory`, `escalation_provider_factory`), defaulting to `providers/factory.py`'s functions; provider selection logic must stay centralized there.

## Guardrails for changes

- Preserve synthetic-only public data. Never copy private assignment files, evidence crops, screenshots, runtime traces, or absolute machine paths into Git.
- Prefer a failing test or a new fixture-backed acceptance case before changing a planner/tool contract.
- Keep valid-but-unsupported, ambiguous, provider error, timeout, and cancellation dispositions distinct. This was a known gap through M1 (documented in SPEC-M1 §4.3) and was fixed in M1.5: `AgentService._unsupported_subresult` now maps the actual underlying cause to the correct disposition (`error` / `clarification_required` / `unsupported`) instead of hardcoding `refused` -- see `docs/decisions/README.md` D-010 for the full mechanism and `tests/test_disposition_contract.py` for the coverage. Do not reintroduce a collapsed disposition when adding a new failure path; map it explicitly.
- Do not add a generic BIM query language, general cross-source joins, compliance reasoning, or long-term memory without an explicit, recorded owner decision (`OD-n`). One narrow, explicit exception exists today: SPEC-M2's door/window reconciliation pilot (OD-15) -- its existence does not authorize broadening cross-source capability further.
- Treat Dockerfiles and OpenAI hooks as unvalidated extension points unless a fresh end-to-end run proves otherwise.
- Do not silently broaden CORS, secrets, persistence, or external network access.

## Current repository state

The public `main` line is well past the initial release: seventeen milestones (`docs/specs/SPEC-M1-*`
through `SPEC-M17-*`) and seventy-three-plus decision-log entries (`docs/decisions/README.md`,
D-001 through D-073 and counting) are merged, including a real, currently-deployed Azure profile
(Container Apps, Azure OpenAI, Azure AI Search, PostgreSQL, ADLS Gen2 multi-project isolation
across four projects -- two synthetic, two real openly-licensed buildings, see README.md's "Real
Dataset Pack" -- Blob Storage, Application Insights) alongside the original local Ollama profile,
plus a V2 tool-calling agent and a persisted, human-reviewable Engineering Finding workflow it can
investigate (SPEC-M11/M16/M17). `PROJECT_STATE.md`'s "Milestone history" section is the
authoritative, dated record -- read it, not just this file's own code map, before assuming a
capability is future work. This handoff is documentation-only and should be developed on a
dedicated branch; it does not authorize a merge or production change. Runtime output under
`runtime/` is local and ignored.

## Takeover questions

Before implementing a non-trivial change, record: the user-visible problem, the invariant being preserved, the acceptance test, the data/secret boundary, and whether the change is a public release claim or only a local experiment. If any of these are unclear, mark the item `REVIEW REQUIRED` instead of redesigning the architecture.
