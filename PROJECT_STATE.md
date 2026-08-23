# Project State

## Purpose and scope

ARMIE Construction Intelligence Workbench is a local, public reference implementation for auditable questions over one synthetic IFC model, one synthetic engineering schedule, and an optional current viewer snapshot. It demonstrates how natural-language interpretation can be separated from deterministic domain execution, evidence, independent verification, and an inspectable audit trail.

This repository is not a production SaaS, compliance engine, unrestricted BIM query language, multi-tenant service, or production data-governance boundary. The public fixture and all committed screenshots are synthetic.

## Current implementation

The checked-in system is a React/Vite browser application backed by FastAPI and a LangGraph `AgentService`:

```text
React workbench
  -> FastAPI request boundary
  -> LangGraph context resolution and routing
  -> heuristic plan or bounded Ollama semantic plan
  -> typed QueryPlan/MultiQueryPlan validation and capability gate
  -> IfcOpenShell / PDF / viewer-snapshot tool
  -> evidence and citations
  -> independent verification
  -> response and audit events
```

The main orchestration lives in `apps/api/app/agent/graph.py`. Shared contracts are in `apps/api/app/schemas/models.py`; routing and canonical plan safety are in `apps/api/app/agent/router.py` and `apps/api/app/agent/plan_validation.py`.

### Implemented reference capabilities

- Synthetic IFC counts, per-storey grouping, selected-element identity, and bounded height extrema for doors and windows.
- Controlled IFC quantity/property resolution with unit normalization and winner-element evidence.
- Synthetic PDF schedule lookup with native extraction where possible and Ollama vision fallback for drawing fields.
- Current-view screenshot inspection with target-visibility and sufficient-view checks; clarification is preferred over an unsupported visual claim.
- Short-term conversational context, explicit clarification, unsupported/refused dispositions, request cancellation/deadlines, citations, grouped audit stages, and independent verification.
- Local Ollama provider path using `qwen3:8b` and `qwen3-vl:8b`. OpenAI provider hooks remain in the provider abstraction, but the public release has not been validated end to end with OpenAI.
- Optional bounded escalation for persistent semantic-plan validation failures: set `OLLAMA_ESCALATION_MODEL` in `.env` to a larger local model to enable it. It is disabled by default; no developer or CI environment is required to hold a large model. Provider access for planning, vision, and escalation goes through factories injected on `ServiceContainer` (`apps/api/app/services.py`), so the probabilistic path can be driven in tests by a fake provider with no live model (see `docs/decisions/README.md` D-007).

The committed public fixture contains two storeys, four synthetic doors, four synthetic windows, controlled quantities, and fictional schedule identifiers. Fixture facts are test/demo data, not claims about a real project.

## Invariants and boundaries

1. The LLM interprets intent and proposes a typed plan; Python/IfcOpenShell remains authoritative for IFC counts, grouping, and controlled arithmetic.
2. Every executable plan passes a capability gate. Unsupported, ambiguous, or unsafe requests must not be converted into a partial numeric answer.
3. An answer should carry source evidence (IFC identity or PDF page/region) and an independent verifier result.
4. Viewer snapshots are observational evidence scoped to the captured view; they do not replace structured IFC identity or geometry queries.
5. Provider autonomy is bounded by timeouts, request IDs, cancellation, typed outputs, and audit events.
6. Public data is synthetic. Private assignment/customer IFC, PDFs, crops, traces, credentials, and local model caches must remain outside Git.

## Known limitations and production gaps

- One active local workspace and one IFC/PDF pair; no durable multi-user conversation store, authentication, tenancy, migrations, or deployment SLOs.
- The IFC query surface is deliberately bounded. It is not an arbitrary property-query language, nearest-room search, cross-source join engine, or compliance engine.
- Viewer geometry is a bounded browser projection of the IFC. Autonomous camera planning and general spatial reasoning are not implemented.
- PDF vision fallback can be slow and probabilistic; evidence and verification are required, but production document-layout/OCR infrastructure is out of scope.
- Conversations and request state are held in process; restart loses active context. The audit trail itself is the exception: it persists to a local JSONL file (`runtime/audit.jsonl`, `AuditStore`), so audit history survives a restart even though conversation context does not.
- The `/api/v1/evidence/{filename}` endpoint enforces basename containment (`Path(filename).name != filename` is rejected) before resolving into `evidence_dir`; this was previously an undocumented invariant rather than a gap.
- OpenAI hooks, LangSmith hooks, and the two API Dockerfiles are extension/development paths, not claims of a separately validated production deployment. No Compose file is present in this repository.
- The graph module is large and carries historical compatibility paths. Refactoring it should preserve the typed plan and verification boundaries.
- The automated public test surface is 122 tests across 7 files (`tests/`; see the "Verification state" section below), CI-verified on Python 3.9-3.12 via `.github/workflows/ci.yml`. This is a deterministic-contract and fake-provider-driven regression net for the router/planning/verification boundary, not a scored or graded evaluation harness; there is no claim of a 216-case/deep evaluation run in this repository.
- CORS is configured for the documented local development origins. A deployment must replace this with an explicit environment-specific policy.
- The `error`/`clarification`/`unsupported` dispositions all currently collapse into `refused` on the semantic-planning failure path (`AgentService._unsupported_subresult` hardcodes `refused` for any `source="unsupported"` subplan; `state["planner_error"]` is set but only read by the unreachable `_refuse` node). This is a partial violation of the D-004 honest-failure invariant. No numeric or factual claim is ever emitted on this path and `VerificationStatus.status` never reaches `passed`, so it is safe but imprecise. `tests/test_failure_path_evals.py`'s F2, F5, and F6 pin the actual behaviour. Leading M1.5 candidate; see `docs/decisions/README.md` D-008 and `docs/decisions/REVIEW_REQUIRED.md`.

## Verification state (this checkout)

The last local verification for the public workspace was:

```text
PYTHONPATH=apps/api python3 -m pytest -q  -> 122 passed
cd apps/web && npm ci && npm run build    -> passed (Vite chunk-size warning only)
```

The 122 tests are: 2 `IfcRepository` fixture tests (`test_public_workspace.py`); deterministic
contract tests for `router.py`, `plan_validation.py`, and `verifiers.py` (no provider); 12
failure-path evals F1-F12 (`test_failure_path_evals.py`) driven by a scripted, no-network fake
provider, asserting the disposition contract in `docs/specs/SPEC-M1-reliability-foundation-v1.md`
§4.3; CJK/multilingual characterization tests that pin, but do not endorse, current behaviour;
and provider-seam-invariance tests. CI (`.github/workflows/ci.yml`) runs this suite on Python
3.9, 3.10, 3.11, and 3.12 on every push and pull request, plus a frontend build and a `ruff`
lint gate. See the SPEC-M1 PR for the run.

The browser screenshots in `docs/images/` are public synthetic-data captures. They are presentation evidence, not a substitute for a fresh browser run after code changes. Live-provider (Ollama/OpenAI) end-to-end behaviour is not exercised by this automated suite; equivalence across `llm_provider` values is asserted at the factory-selection and audit-field level only (`test_provider_seam_invariance.py`).

## Local operation

```bash
cp .env.example .env
ollama pull qwen3:8b
ollama pull qwen3-vl:8b

python3 -m venv .venv
source .venv/bin/activate
pip install -e 'apps/api[dev]'
PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
cd apps/web
npm ci   # or: npm install
npm run dev
```

Both `npm ci` and `npm install` work with no flags (`three` is pinned to `0.149.0` to match `web-ifc-three`'s peer requirement; see `docs/decisions/README.md` D-006). Use the Vite URL printed by the command, normally `http://127.0.0.1:5173`. Set `API_PROXY_TARGET` when the API uses a different port. Runtime traces/evidence are intentionally ignored by Git.

## Milestone history

**M1 — Test Seam, Reproducible Build & Deterministic Behavioural Contract** is complete and submitted via PR #1 (`docs/specs/SPEC-M1-reliability-foundation-v1.md`). It established: a reproducible `npm ci`/`npm run build` (D-006); an injected provider-factory seam on `ServiceContainer` so the probabilistic path can be driven by `FakeModelProvider` with no live model (D-007); 122 deterministic and failure-path tests (`tests/`) covering the router/plan-validation/verification boundary and the F1–F12 canonical disposition contract (D-008); CI on the Python 3.9–3.12 matrix plus a frontend build and lint gate (`.github/workflows/ci.yml`); and corrected documentation claims (the false fixture-coverage claim, audit persistence, evidence-endpoint containment, the opt-in escalation setting).

M1 deliberately did **not** fix either defect its own failure-path tests found (the disposition-category collapse described below, and a latent `heuristic_multi_plan` crash — see `docs/decisions/REVIEW_REQUIRED.md`), and did not restructure `graph.py`. Both were out of M1's "call sites only" / declared-file-surface scope; fixing them was left to a later milestone with the regression net already in place.

## Next milestone (M1.5 — scope not yet approved)

Candidate scope: a narrowly-scoped repair of the disposition collapse (`error`/`clarification`/`unsupported` all currently surfacing as `refused`) and the `_refuse`-node unreachability behind it (`docs/decisions/README.md` D-008 "Current conformance"), plus only the local `graph.py` control-flow decoupling that fix requires — explicitly **not** a full `graph.py` decomposition. Rationale: this restores the D-004 honest-failure invariant the repository currently declares but does not fully honour, and it should precede capability expansion, since any new capability added on top of the current planning/execution path would inherit the collapsed disposition layer.

## Deferred beyond M1.5

Dead `web-ifc`/`web-ifc-three` removal, `graph.py` decomposition more broadly, the `ResponseLanguage` (`pt-PT`/`fr`/`es`) contract decision, and the deployment/security boundary — all tracked in `docs/decisions/REVIEW_REQUIRED.md`.

No scope beyond what is explicitly stated above is approved by this document; product scope and acceptance criteria must be agreed before implementation.
