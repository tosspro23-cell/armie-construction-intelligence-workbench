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
- Conversations and request state are held in process; restart loses active context.
- OpenAI hooks, LangSmith hooks, and the two API Dockerfiles are extension/development paths, not claims of a separately validated production deployment. No Compose file is present in this repository.
- The graph module is large and carries historical compatibility paths. Refactoring it should preserve the typed plan and verification boundaries.
- The current automated public test surface is intentionally small (two tests); there is no CI workflow or claim of a 216-case/deep evaluation run in this repository.
- CORS is configured for the documented local development origins. A deployment must replace this with an explicit environment-specific policy.

## Verification state (this checkout)

The last local verification for the public workspace was:

```text
PYTHONPATH=apps/api python3 -m pytest -q  -> 2 passed
cd apps/web && npm run build              -> passed (Vite chunk-size warning only)
```

The browser screenshots in `docs/images/` are public synthetic-data captures. They are presentation evidence, not a substitute for a fresh browser run after code changes.

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
npm install
npm run dev
```

Use the Vite URL printed by the command, normally `http://127.0.0.1:5173`. Set `API_PROXY_TARGET` when the API uses a different port. Runtime traces/evidence are intentionally ignored by Git.

## Recommended next milestones

The next agent should first reproduce the tests/build and a synthetic browser smoke path, then choose one bounded improvement at a time. Candidate work includes expanding fixture-backed tests, isolating graph stages, adding CI, and defining a deployment/security boundary. None is approved by this document; product scope and acceptance criteria must be agreed before implementation.
