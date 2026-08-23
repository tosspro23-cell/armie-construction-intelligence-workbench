# Agent Handoff

This is a cold-start guide for a new vendor or frontier coding agent. Read this file together with [`PROJECT_STATE.md`](PROJECT_STATE.md), [`README.md`](README.md), [`docs/architecture.md`](docs/architecture.md), and [`SECURITY_AND_DATA.md`](SECURITY_AND_DATA.md) before editing code.

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
| Ollama/OpenAI provider boundary; centralized provider selection | `apps/api/app/providers/`, `apps/api/app/providers/factory.py` |
| Provider **factory** injection seam (text/vision/escalation), audit store, tool services | `apps/api/app/services.py` (`ServiceContainer`) |
| IFC deterministic adapter | `apps/api/app/tools/ifc/repository.py` |
| PDF rendering/native lookup/vision preparation | `apps/api/app/tools/document/analyzer.py` |
| Independent and invariant verification | `apps/api/app/verification/verifiers.py` |
| Browser state, viewer, citations, audit grouping, cancellation | `apps/web/src/main.tsx`, `apps/web/src/IfcViewer.tsx`, `apps/web/src/styles.css` |
| Deterministic-contract, failure-path, characterization, and seam-invariance tests | `tests/` (see `docs/specs/SPEC-M1-reliability-foundation-v1.md`) |
| The only fake used to drive the probabilistic path in tests | `tests/fakes/fake_provider.py` (`FakeModelProvider`) |

## End-to-end lifecycle

`POST /api/v1/chat` creates a request-scoped invocation. LangGraph resolves viewer/conversation context, selects a deterministic or semantic plan, canonicalizes and validates it, checks capability, executes a source-specific tool, creates evidence, verifies the result, and returns an `AgentResponse`. The API commits context updates only for terminal answered/clarification flows. `/api/v1/traces/{trace_id}` exposes the audit events used by the UI. `/api/v1/requests/{request_id}/cancel` is the cancellation boundary.

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
- Keep valid-but-unsupported, ambiguous, provider error, timeout, and cancellation dispositions distinct. Known gap: `AgentService._unsupported_subresult` currently collapses parse-failure/repair-exhausted, escalation-unavailable, and transport-error outcomes to `disposition="refused"` instead of the normatively distinct `error`/`clarification` categories (SPEC-M1 §4.3, defect findings in the SPEC-M1 PR) -- still safe (no numeric claim), but do not "fix" this without reading that writeup first; it is a `graph.py` control-flow change, out of the SPEC-M1 M1 "call sites only" scope.
- Do not add a generic BIM query language, cross-source joins, compliance reasoning, or long-term memory without an explicit scope decision.
- Treat Dockerfiles and OpenAI hooks as unvalidated extension points unless a fresh end-to-end run proves otherwise.
- Do not silently broaden CORS, secrets, persistence, or external network access.

## Current repository state

The public `main` line contains the initial public release plus synthetic screenshot documentation. This handoff is documentation-only and should be developed on a dedicated branch; it does not authorize a merge or production change. Runtime output under `runtime/` is local and ignored.

## Takeover questions

Before implementing a non-trivial change, record: the user-visible problem, the invariant being preserved, the acceptance test, the data/secret boundary, and whether the change is a public release claim or only a local experiment. If any of these are unclear, mark the item `REVIEW REQUIRED` instead of redesigning the architecture.
