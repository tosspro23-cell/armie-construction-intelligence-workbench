# ARMIE Construction Intelligence Workbench — Cold Handoff Assessment

**Branch:** `chore/vendor-neutral-agent-handoff`
**Commit:** `1936d3f456577c4527aa3f38ec15f2ce7aa2b1b1`
**Assessed by:** Claude (Cowork), cold cross-vendor takeover exercise
**Date:** 2026-08-23

---

## 1. Takeover confidence: **High**

The documentation set is unusually calibrated rather than promotional — `PROJECT_STATE.md` volunteers its own weaknesses (two tests, no CI, a large orchestration file, unvalidated Docker/OpenAI paths) instead of hiding them. Every architectural claim I checked against the actual code held up, the documented reproduction commands worked (with one undocumented gap, below), and I was able to run a real end-to-end request through a live browser against the live backend and get exactly the documented behavior. The main risk to a new agent isn't missing knowledge — it's the density and ad hoc special-casing inside one 1,203-line file (`graph.py`), including at least one clear violation of a stated design principle (see §5). None of this blocks a confident takeover; it just means the first bounded milestone should be defensive (tests), not additive (features) — see §9.

## 2. Repository verification

- **Branch/commit:** confirmed exact match — `git log -1` on the checked-out branch shows `1936d3f456577c4527aa3f38ec15f2ce7aa2b1b1`.
- **Repo shape:** 3 commits total on this branch (`f9a579a` initial public release → `5eb9d75` screenshots → `1936d3f` this handoff). Backend is ~3,700 LOC across `apps/api/app`; frontend is 3 files (~580 LOC).
- **Backend tests:** `pip install -e 'apps/api[dev]'` succeeded cleanly. `PYTHONPATH=apps/api python3 -m pytest -q` → **2 passed**, exactly matching `PROJECT_STATE.md`'s claimed verification state. Both tests exercise `IfcRepository` only (door count, per-storey window grouping, unit-normalized quantity aggregation).
- **Frontend build — undocumented gap found:** `npm install` and `npm ci` (against the committed lockfile) **both fail** with an `ERESOLVE` peer-dependency conflict: `web-ifc-three@0.0.126` requires `three@^0.149.0`, but the project pins `three@0.155.0`. This reproduced on Node 22.22.2 / npm 10.9.7. `npm install --legacy-peer-deps` resolves it, after which `npm run build` succeeds exactly as documented — Vite chunk-size warning only, 619 KB main bundle. **No doc mentions this flag is required.** This is a real, reproducible cold-start friction point, not a one-off environment quirk (it fails identically against the locked dependency graph).
- **Smoke test:** Ollama is not installed and not reachable from this sandbox (no local process, no egress to ollama.com), so LLM-dependent paths could not be exercised live. I instead ran the deterministic/heuristic core end-to-end:
  - Started FastAPI + the real `IfcRepository`/`DocumentAnalyzer` against the committed synthetic fixtures.
  - `curl` to `/api/v1/chat` with "How many doors are in the model?" → correct answer (4), 4 citations with real GlobalIds, verification `passed`, 0 model calls, full audit trail.
  - "How many windows are on each level?" → `Level 01: 2; Level 02: 2` (matches the unit test's assertion exactly), 17 distinct audit events across the documented stages.
  - Started the Vite dev server and drove the **real React UI in headless Chromium**: the 3D viewer rendered actual IfcOpenShell-derived geometry from the synthetic model (visibly matching `docs/images/workbench-overview.png`), and submitting a chat question produced the correct answer with Evidence Inspector and a correctly-staged Audit Trail (Decision Summary, grouped stages) — a genuine full-stack reproduction, not just an API check.
  - A PDF field lookup that requires the vision fallback (`diversity factor for Panel-A`) correctly failed with `disposition="error"` ("All connection attempts failed") rather than guessing — this is a positive finding: it confirms the "honest failure over confident hallucination" invariant holds even when the LLM backend is entirely absent.

## 3. Independent architecture reconstruction

**Problem it solves:** auditable Q&A over exactly one synthetic IFC model, one synthetic PDF schedule, and an optional viewer screenshot — deliberately not a general "chat with your BIM" system. The organizing bet is that natural language should only ever *interpret intent*, never *compute facts*.

**Frontend/backend boundary:** the React/Vite SPA (`apps/web/src/main.tsx`, `IfcViewer.tsx`) is almost entirely a rendering and state client — it formats server responses, manages tabs/selection/citation-focusing, and does client-side 3D rendering (three.js/web-ifc-three) of a bounded geometry projection the server computes from the real IFC. There is no planning, verification, or domain logic on the client. Everything else lives server-side.

**Request lifecycle:** `POST /api/v1/chat` wraps `AgentService.invoke` in `asyncio.wait_for` with a 180s deadline, run in a thread; a parallel `/api/v1/requests/{id}/cancel` endpoint cancels the tracked asyncio task. Internally, a LangGraph state machine runs `resolve_context → route → execute_multi → finalize` (with a `refuse` branch), where `execute_multi` fans out to `execute_ifc` / `execute_pdf` / `execute_viewer` per subplan. Every node appends typed `AuditEvent` records to an append-only JSONL store, replayable by `trace_id` via `/api/v1/traces/{id}` — verified live (17 events for one grouped query).

**QueryPlan / MultiQueryPlan:** `QueryPlan` is the atomic typed task (source/intent/operation/entity_type/filters/group_by/postprocess/expected_result_shape/...); `MultiQueryPlan` wraps 1–8 subplans plus language/interpretation metadata. This is real and verified: both the heuristic path and the LLM path converge on the identical Pydantic contract before anything executes.

**Heuristic vs. semantic planning:** `fast_path_coverage`/`heuristic_plan`/`heuristic_multi_plan` (`router.py`) match narrow, ASCII-only regex templates. Anything else — compound requests, non-English, follow-ups needing real language understanding — goes to the Ollama-backed semantic planner (`provider.structured(response_model=MultiQueryPlan)`), followed by a substantial canonicalization/repair/escalation cascade in `plan_validation.py` that treats "schema-valid but semantically wrong LLM output" as an expected failure mode, not an edge case (e.g., recovering a dropped `group_by`, rejecting execution modifiers smuggled into `filters`, escalating twice — once to a repair prompt, once to a larger local model — before giving up into a clarification).

**Capability gating:** `capability_gate()` runs after canonicalization against a small explicit allow-list (six IFC entity types, controlled `aggregate_quantity` measures, constrained `space_distance`). A syntactically valid plan for an unsupported combination is explicitly rejected, not silently downgraded — verified in code and consistent with D-002/D-004.

**Deterministic IFC execution:** `IfcRepository` wraps `ifcopenshell` directly; counts, storey grouping, quantity aggregation (with unit normalization), and bounded space-distance all run in pure Python over parsed entities. No LLM ever touches a numeric IFC fact — verified live (door count = 4, matching the fixture and the unit test).

**Engineering-document processing:** `DocumentAnalyzer` tries cheap native PyMuPDF text matching first; below a confidence threshold (or for distribution-board-pattern questions) it runs a genuinely more rigorous vision pipeline than a naive "send the PDF to GPT-4V": localize the board region → crop → extract candidate value from the crop → **independently re-verify the same crop with a second model call**. This grounding-then-independent-recheck pattern is real engineering discipline, not a demo shortcut.

**Viewer/snapshot handling:** a captured screenshot is sent to a vision model twice — once to interpret, once to independently verify the same claim against the same image — and both must agree, with occlusion/sufficient-view checks, before an "answered" disposition is allowed. Snapshot evidence is explicitly scoped to the captured view, not treated as IFC ground truth.

**Evidence and citations:** `Evidence` carries a locator (IFC GlobalId/ExpressID, PDF page/bbox/field, or snapshot id/camera pose); `Citation` is the UI-facing projection. The UI can click a citation to re-focus the corresponding IFC element, PDF crop, or snapshot.

**Independent verification:** distinct from the answer generator in every path — an independent deterministic recomputation must literally match the primary IFC result (`DeterministicVerifier`); an `InvariantValidator` refuses "answered" without evidence *and* citations; an execution-consistency check confirms the tool actually preserved the validated plan's shape; PDF/vision paths get a second, independently-prompted verification call rather than trusting one pass.

**Provider/model abstraction:** a `ModelProvider` Protocol (`structured`, `vision_structured`) with `OllamaProvider` and `OpenAIProvider` implementations, selected by a `llm_provider` setting (`ollama` / `openai` / `hybrid` — hybrid means Ollama text + OpenAI vision, a detail nowhere explained in prose). The OpenAI path is structurally real (Responses API, strict JSON schema) but, as the docs accurately disclose, unvalidated end-to-end in this release — I could not test it either (no API key), consistent with that disclaimer.

**Cancellation/deadline:** real and verified in code — an in-memory per-request task registry, a dedicated cancel endpoint, and a hard `asyncio.wait_for` deadline, both producing distinct terminal dispositions (`CANCELLED`, `TIMEOUT`) separate from `ERROR`/`REFUSED`.

**Auditability/traces:** 35+ typed `AuditEvent` event kinds, grouped by the UI into six human-readable stages with a raw-JSON fallback — a real observability pattern, not just logging. Verified live.

### Implemented vs. bounded vs. partial vs. gaps vs. future

- **Implemented & verified:** deterministic IFC query engine; typed plan contract + validation/capability gate; evidence+citation plumbing; deterministic self-verification; audit trail; cancellation/timeout; React viewer with real (bounded) IFC geometry.
- **Implemented, code-verified but not live-exercised (no LLM available):** semantic planner repair/escalation cascade; PDF vision pipeline; viewer-snapshot vision pipeline; OpenAI provider.
- **Bounded/demo by design (honestly documented):** single active project workspace (one IFC + one PDF); six-entity-type ontology; no cross-source joins (actively detected and refused); no nearest-room search (actively detected and refused); viewer geometry is a bounded proxy set, not the full model.
- **Partially implemented / unvalidated extension points:** OpenAI provider, LangSmith tracing hooks, two Dockerfiles with no Compose file, multilingual support (see §5 — this is closer to targeted patches than a general i18n design).
- **Production gaps (documented and independently confirmed):** in-memory-only conversation/request state; no auth/tenancy/rate limiting; no CI; hardcoded 3-origin CORS; a 1,203-line orchestration file carrying substantial special-case logic that is almost entirely untested.

## 4. Handoff quality — what was done well

- Six governing documents (`README`, `PROJECT_STATE`, `AGENT_HANDOFF`, `CLAUDE.md`, `docs/architecture.md`, `docs/decisions/README.md`) tell a **consistent** story with no contradictions between them.
- `PROJECT_STATE.md` is unusually self-critical: it names its own test count, explicitly disclaims any larger evaluation suite, flags `graph.py`'s size and "historical compatibility paths," and explicitly labels Dockerfiles/OpenAI/LangSmith as unvalidated extension points.
- `AGENT_HANDOFF.md`'s code map is accurate — every file it names exists and does what it says.
- The five-item decision log (D-001–D-005) is short, principle-based, and matches the code I read: the determinism boundary, the capability gate, evidence-before-verification, honest-failure dispositions, and synthetic-data-only were all verifiable, not just asserted.
- Guardrails are genuinely actionable ("prefer a failing test before changing a planner contract," "don't add cross-source joins without a scope decision") rather than generic boilerplate.
- The handoff is authentically vendor-neutral: I found no tooling, prompts, or references specific to the prior coding agent baked into docs or code.

## 5. Handoff discrepancies

- **README overclaims test coverage.** It states public tests "cover planning, IFC tools, document safety, verification, and provider parsing." In fact there are exactly two tests, both exercising `IfcRepository` directly. Nothing touches `router.py`, `plan_validation.py`, `verifiers.py`, `providers/`, or `document/analyzer.py`. `PROJECT_STATE.md`'s "intentionally small (two tests)" is accurate; this README sentence is the discrepancy, and it's the kind of claim that could make a new agent underestimate regression risk.
- **`npm install` (and `npm ci`) fails on a stock recent Node/npm** with an `ERESOLVE` conflict between the pinned `three@0.155.0` and `web-ifc-three@0.0.126`'s `three@^0.149.0` peer requirement. This reproduces against the *committed lockfile*, so it isn't environment noise. `--legacy-peer-deps` fixes it and the rest of the documented build path works exactly as claimed. Nowhere is this flag mentioned.
- **A stated architectural principle is violated in at least one place.** `router.py` comments state reference resolution is "deliberately *not* implemented with pronoun or language lookup tables." But `graph.py._resolve_context` contains a hardcoded literal-string check for a specific Chinese phrase ("这张图里的板有多少"), and `router.py`/`graph.py` carry several more Chinese-specific term lists and a hand-built Chinese noun-mapping dictionary in the answer renderer. This is understandable pragmatic bilingual support, but it's undocumented anywhere, untested, and structurally the opposite of the stated no-keyword-routing principle in that one path — exactly the kind of thing a new agent could silently break while refactoring `graph.py` without realizing bilingual behavior depended on it.
- **Minor omission, not a contradiction:** `SECURITY_AND_DATA.md` says the app is "not a security boundary," which is true and appropriately scoped, but doesn't mention that `main.py`'s evidence-file endpoint *does* implement basename-only path-traversal protection (`Path(filename).name != filename`). Not a discrepancy so much as a detail a new agent touching that endpoint should know exists.
- **Undocumented term:** "hybrid" provider mode (Ollama text + OpenAI vision) is used in code (`providers/factory.py`) and mentioned in `PROJECT_STATE.md`'s prose, but never defined precisely enough that a reader would know which model handles which modality without reading the factory code.

## 6. Production-readiness assessment (ranked)

1. **All state is in-process memory** — conversations, active requests, and even the audit trail (append-only JSONL with no rotation) don't survive a restart or scale past one process.
2. **No auth, tenancy, or rate limiting** — anything reaching the API can query or cancel any `request_id`.
3. **No CI, and the existing 2 tests cover a small fraction of the codebase** — `router.py`, `plan_validation.py`, `verifiers.py`, `providers/`, and `document/analyzer.py` (the majority of the interesting logic) have zero automated coverage today.
4. **Single active workspace** — the `ProjectWorkspace`/`SourceRegistry` abstraction the architecture doc gestures at doesn't exist yet; this is the main blocker to any real multi-project use.
5. **Hard LLM dependency for anything off the narrow heuristic templates**, with no offline/degraded mode beyond the deterministic fast paths (though failure is honest, not hallucinated, when the LLM is unreachable — verified live).
6. **`graph.py`'s size and density is itself a production risk** — 1,203 lines with heavy inline special-casing (including the undocumented bilingual coupling above) make it expensive to review or extend safely without much stronger test coverage first.
7. **CORS, Docker, and OpenAI paths are explicit unvalidated extension points**, not production deployment options, exactly as the docs say.

## 7. Construction business-value assessment

The most credible value isn't "chat with your BIM" — it's a **trust layer for BIM/document QA**: catching and defensibly explaining model-vs-schedule mismatches (door/window counts, quantity extrema, schedule field lookups) with citations an engineer could actually put in a submittal or RFI response, rather than a chatbot's unverifiable prose. That's a real, common pain point — coordination and QA gaps between BIM authors and discipline-specific schedules are a well-known source of rework and RFIs.

Where it currently falls short of that value: it explicitly refuses the most valuable cross-referencing question ("does the schedule's connected load match what's modeled"), it's scoped to exactly one project's worth of source files, and it has no integration with real authoring tools (Revit/Navisworks) or issue tracking (BCF). Today it's a standalone, single-project Q&A proof of concept for the trust-layer idea — a credible seed, not close to deployable value on its own. The evidence/verification architecture is the part worth defending and building on; the product surface around it is still a toy.

## 8. Portfolio-value assessment

What this project genuinely proves, at a level uncommon in portfolio work:

- **Typed agent architecture with a real, enforced boundary between probabilistic planning and deterministic execution** — not just prompt engineering, but a defensible pattern for where an LLM should and shouldn't be trusted.
- **Deterministic + probabilistic hybrid done with discipline** — heuristic-first routing, LLM fallback, and a repair/escalation cascade that treats "schema-valid but semantically wrong" LLM output as an expected failure mode to defend against.
- **Evidence/provenance and independent verification actually implemented**, not just described — an independent recount for every deterministic answer, a second independent vision pass over the same crop for document extraction, invariant checks that block "answered" without evidence.
- **A real multimodal grounding pipeline** (localize → crop → extract → independently reverify) that's more rigorous than a single vision call.
- **Genuine auditability** — a structured, replayable, UI-groupable trace, not log lines.

What it doesn't yet demonstrate: a formal evaluation/regression harness (the docs are honest there isn't one), production observability/metrics beyond a stub, concurrency/scale handling, or provider cost/latency tradeoff analysis. A senior reviewer's first question — "how do you know this doesn't regress?" — currently has the honest answer "two unit tests on one module."

## 9. Recommended next milestone

**Build an automated evaluation/regression harness for the planning and verification pipeline (fixture-backed tests + a mock `ModelProvider` + minimal CI), rather than a new feature.**

- **Business value:** every future change to a system meant to produce citable, trustworthy answers is currently unguarded outside `IfcRepository`. Reducing silent-regression risk directly serves the "trusted project decisions" value proposition — a QA tool that can silently regress is worse than no QA tool.
- **Architecture value:** exercises and hardens exactly the typed-contract, capability-gate, and verification boundaries the whole system is organized around, without touching product scope — the lowest-risk, highest-leverage next step given how well-typed and testable this codebase already is (Pydantic contracts, pure functions in `plan_validation.py`, a deterministic `IfcRepository`).
- **Portfolio value:** "built the evaluation harness for a hybrid deterministic/probabilistic system" is a rarer, stronger senior-engineering signal than another demo feature.
- **Implementation scope:**
  - Table-driven tests for `router.py` (`heuristic_plan`, `heuristic_multi_plan`, `capability_gate`, `fast_path_coverage`) against the existing demo fixtures — no new fixtures needed.
  - Unit tests for the pure functions in `plan_validation.py` (`canonicalize_subplan`, `validate_multi_plan`, `verify_execution_consistency`, `eligible_scalar_count_batch`).
  - Tests for `verifiers.py` against the real `IfcRepository` fixture already used by the two existing tests.
  - A fake `ModelProvider` implementing the existing Protocol, enabling deterministic tests of the semantic-planning path in `graph.py` without a live Ollama instance — this is also what makes CI possible at all.
  - A minimal CI workflow running `pytest -q` and `npm run build` on push, including the `--legacy-peer-deps` fix found in this session.
  - Explicitly out of scope: no product-facing feature, no schema change, no attempt to fix or generalize the bilingual-support code found in §5 — document it, don't redesign it, in this milestone.
- **Acceptance criteria:** previously-zero-covered modules (`router.py`, `plan_validation.py`, `verifiers.py`) reach meaningful line coverage without weakening any existing invariant; CI is green on `pytest -q` and `npm run build` in a fresh checkout; a fake provider exercises at least one full semantic-planning-to-verification path with no live LLM; `README.md`/`PROJECT_STATE.md` are corrected to state actual test scope truthfully.
- **Major risks:** scope creep into "fixing" the bilingual hardcoding while writing tests around it (resist — document, don't redesign, this round); a fake provider that's too well-behaved and misses real quirks like the Ollama `thinking`-vs-`response` field workaround — tests must include malformed/edge-case model outputs, not just happy paths, or the harness gives false confidence.

## 10. Alternative milestones

**A. Multi-project workspace foundation** (`ProjectWorkspace`/`SourceRegistry`, gestured at in `docs/architecture.md`). Higher long-term business-value ceiling, but larger scope and more invasive to the typed-plan/capability-gate boundary — better sequenced *after* the eval harness exists to protect it.

**B. Cross-source verification** (e.g., confirm a schedule-referenced board maps to a real IFC electrical element). This is the highest-value differentiator for the actual "coordination/rework reduction" pain point, but it's explicitly out-of-scope today (`cross_source_join_requested` is a deliberate refusal gate) and needs a genuine product-scope decision from the owner first, per `AGENT_HANDOFF.md`'s own "Takeover questions" guidance.

## 11. Open owner/domain questions

- Is the near-term goal a portfolio artifact, an early product, or both? This determines whether the eval-harness milestone or the cross-source-join milestone should come first.
- Should Chinese-language support be a maintained, tested capability going forward, or was it exploratory scaffolding safe to simplify? It's currently undocumented, untested, and embedded ad hoc in core orchestration — someone needs to decide its status before a `graph.py` refactor risks silently breaking or silently preserving fragile code.
- Is the OpenAI provider meant to become an equally-supported path worth its own test investment, or a lower-priority future option? This affects whether the eval-harness milestone should build OpenAI-specific fakes too.
- Is the bounded/single-IFC-file demo scope permanent, or is production-scale IFC (tens of thousands of elements, real property-set variability) an eventual target? This materially affects whether performance/streaming work belongs in the next few milestones.
- Should the discrepancies found in §5 (README test-coverage claim, missing `--legacy-peer-deps` note) be corrected now as a small, low-risk documentation-accuracy commit, or held for a dedicated cleanup the owner reviews separately? I made no source or doc changes this session per the stop condition — flagging this as ready to do next if approved.

---

*No production or documentation source files were modified during this session. This assessment is provided for milestone approval before any implementation work begins.*

---

## Project Control addendum (2026-08-24)

The body above is preserved unchanged as a historical record, including its errors. This addendum records four things established after the fact.

1. **One factual correction.** §6 item 1 states that the audit trail does not survive a restart. That is wrong: `AuditStore` persists to a local JSONL file (`runtime/audit.jsonl`), so audit history survives a restart. Only conversation and in-flight request state are held in process and are lost on restart. This was independently caught during a later cross-vendor review and confirmed by inspection of `AuditStore`.

2. **Convergence.** Three independent agents — this assessment, a Codex cross-vendor review, and Project Control working from a fresh clone — reached the same conclusions on the `ERESOLVE` peer conflict (§2/§5), the README coverage overclaim (§5), the CJK hardcoding (§5), and the path-traversal documentation gap (§5), and on the recommendation that the first milestone be defensive rather than additive (§9). §10-B's cross-source verification is now the approved M2 direction.

3. **A prescient risk, half of which still stands.** §9 warned that a fake provider could be too well-behaved and miss real quirks such as the Ollama `thinking`-vs-`response` field handling. M1 covered the malformed-output half of that risk (F1–F3 in `tests/test_failure_path_evals.py`, exercising unparseable/malformed structured output through the pipeline). But `OllamaProvider`'s own response parsing still has zero test coverage: `FakeModelProvider` exercises the pipeline around a provider, not the provider's own parsing layer, so the `thinking`-vs-`response` field handling this section specifically named is still untested by construction. Recorded as an open gap.

4. **One finding this assessment caught that Project Control missed.** §5's hardcoded Chinese literal in `AgentService._resolve_context` was not found during Project Control's own CJK survey. M1's characterization tests cover it directly (`tests/test_language_selection_characterization.py::test_hardcoded_chinese_board_ambiguity_clarification`).
