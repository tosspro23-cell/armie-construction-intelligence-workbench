# Decision Log

This page records the small set of decisions that define the public reference release. It is not an approval ledger for future product work.

## D-001 — Deterministic facts remain authoritative

IFC counts, storey grouping, controlled quantity resolution, unit conversion, and arithmetic run in Python/IfcOpenShell. Models interpret a request and explain verified results; they do not calculate or invent BIM facts.

## D-002 — One typed plan contract and a capability gate

Heuristic and bounded semantic planning converge on `QueryPlan`/`MultiQueryPlan`. Cross-field validation and the capability gate run before controlled tools. A valid plan may still be explicitly unsupported; it must not be silently downgraded to an unrelated operation.

## D-003 — Evidence precedes verification and presentation

Tool results produce `Evidence` and `Citation` locators. Independent deterministic, evidence, or visual checks produce `VerificationStatus`. The UI exposes grouped stages and keeps raw payloads in a collapsed developer view.

## D-004 — Honest failure is a feature boundary

Ambiguous questions clarify; valid unsupported capabilities refuse with an explanation; provider/transport failures are errors; cancellation and deadlines are terminal states. The system must not turn missing evidence into a confident answer.

This invariant is currently only partially honoured: `error`, `clarification`, and `unsupported` collapse into a single `refused` disposition on the semantic-planning failure path. Safety holds (no numeric or factual claim is ever emitted on this path), but the distinct-disposition half of this invariant does not. See D-008's "Current conformance" section for the mechanism and the regression tests that pin it.

## D-005 — Public repository uses synthetic fixtures only

`demo_data/` and committed screenshots are synthetic/public-safe. Supplied assignment/customer material, derived crops, runtime traces, credentials, and local model caches are excluded. This repository is a local reference workbench, not a security or privacy boundary.

## D-006 — Frontend dependency resolution pins `three@0.149.0`

`apps/web/package.json` pinned `three@0.155.0` while `web-ifc-three@^0.0.126` peer-requires `three@^0.149.0`. On a clean checkout this made both `npm install` and `npm ci` fail (`ERESOLVE`), so the only working install path was `--legacy-peer-deps`, which is not a reproducible build.

The viewer (`apps/web/src/IfcViewer.tsx`) only uses core Three.js APIs (`Scene`, `PerspectiveCamera`, `WebGLRenderer`, `BoxGeometry`, orbit controls) that are stable across 0.149–0.155; it does not import `web-ifc` or `web-ifc-three` (see M1.5 candidate below). M1 pins `three@0.149.0` and `@types/three@^0.149.0`, regenerates `package-lock.json`, and verifies `npm ci` (no flags), `tsc -b`, and `vite build` all pass. Production bundle size drops 619 kB → 585 kB as a side effect of resolving to the older, smaller `three` release; no functional or rendering change was made.

M1 does not migrate the IFC/browser dependency stack. `web-ifc`, `web-ifc-three`, and the copied WASM/worker assets are retained unmodified; whether they are needed for a future client-side IFC parsing path or are removable dead weight is deferred to M1.5 (`docs/decisions/REVIEW_REQUIRED.md`).

## D-007 — Provider injection seam and the deterministic fake as the probabilistic test boundary

Before M1, `ModelProvider` instances were constructed inline inside `AgentService` graph methods (`get_text_provider` / `get_vision_provider` called directly at the call site), so no planner, repair, or escalation path could be exercised without a live Ollama daemon. This made every reliability claim about the probabilistic path unfalsifiable.

M1 adds `model: str` to the `ModelProvider` Protocol (both concrete providers already satisfied it structurally; this only makes the existing dependency explicit) and injects provider **factories** — not provider instances — through `ServiceContainer`, defaulting to the existing `get_text_provider` / `get_vision_provider`. Provider selection logic stays centralized in `providers/factory.py`; the seam changes who calls the factory, never how it chooses. `llm_provider` handling, timeout propagation, bounded-repair/escalation ordering, and audit field semantics are unchanged (verified by `tests/test_provider_seam_invariance.py`, §4.7).

`tests/fakes/fake_provider.py` provides `FakeModelProvider`, a scripted, no-network implementation of `ModelProvider` used to drive every failure-path eval (F1–F12). It is the only mechanism used to exercise the probabilistic path in tests; production `app.providers.factory` globals are never monkeypatched as a substitute, because that would leave the real injection seam untested.

## D-008 — Canonical disposition contract

M1 makes the mapping from failure condition to response disposition explicit and normative (`docs/specs/SPEC-M1-reliability-foundation-v1.md` §4.3): transport failure/timeout/unavailability → `error`; unparseable output after bounded repair → `error`; unsupported capability → `unsupported`/refusal with rationale; ambiguous request → `clarification`; failed verification → not `answered`; client cancellation → `cancelled` with no conversation-context mutation; deadline exceeded → a timeout state distinct from `error`. No branch may emit a numeric or factual claim without evidence, citations, and a passing `VerificationStatus`. F1–F12 (§4.5) test this contract directly; any implementation divergence found while testing is a defect finding, not a reason to adjust a test.

### Current conformance

As of this milestone, production does **not** fully satisfy this contract: `error`, `clarification`, and `unsupported` all currently surface as disposition `refused` on the semantic-planning failure path, rather than as the three distinct categories above.

The root cause is in `apps/api/app/agent/graph.py`. `AgentService._route` sets `state["planner_error"]` when structured-output parsing fails and bounded repair is also exhausted, but that key is only ever read by the `_refuse` node. `_refuse` is reachable solely from `AgentService._after_context`'s conditional edge (triggered by `state["unsupported_reason"]`, set only for a cross-source-join question) — it is unreachable once the static `route → execute_multi` edge has already run, which is the only path that ever sets `planner_error`. Separately, `AgentService._unsupported_subresult` hardcodes `disposition="refused"` for any subplan with `source="unsupported"`, regardless of whether the underlying cause was a genuinely unsupported capability, an exhausted repair, an exhausted escalation, or a raw transport error.

Safety is preserved throughout: no numeric or factual claim is ever emitted on this path, and `VerificationStatus.status` never reaches `passed`. Only the disposition *category* is collapsed. `tests/test_failure_path_evals.py`'s F2 (malformed output, repair exhausted), F5 (escalation configured but unavailable), and F6 (transport error) all assert this actual, safe behaviour and document the divergence in their docstrings, so the eventual fix is regression-protected rather than discovered again from scratch. See `docs/decisions/REVIEW_REQUIRED.md` for this milestone's M1.5 candidate status.

## D-009 — Deterministic document extraction is authoritative; vision is bounded fallback

This is the document-side application of D-001. Before M2 Phase 1, `DocumentAnalyzer.native_lookup` could never succeed by construction: it hardcoded `confidence = 0.55` on a substring match against `pdf_confidence_threshold`'s default of `0.75` (`docs/reports/2026-08-23-pdf-extraction-diagnostic.md`), and `graph.py`'s `_execute_pdf` additionally short-circuited straight to vision whenever both a board and a field were heuristically detected, so `native_lookup` was never even reached for the two of the fixture's three boards that matched `target_board`'s regex. Every document answer came from the Ollama vision model, even though the fixture has a clean, fully machine-readable text layer — a direct inversion of D-001 on the document side.

M2P1 (`docs/specs/SPEC-M2P1-deterministic-document-extraction-v1.md`) replaces line-substring matching with row- and column-aware extraction: words are clustered into rows by `y` and into column bands, derived from the header row's `x` extents, by `x` — both within a tolerance, not exact coordinate equality. `native_lookup` locates the row whose first-column identifier (document-derived, per OD-9 — `target_board`'s regex is not widened) matches the question and the column whose header matches the requested field, and returns that cell's real text and bbox. Confidence reflects what was actually established (OD-11): unambiguous match `0.95` (clears the unchanged `0.75` threshold), ambiguous `0.4`, a field genuinely absent from the document `0.0`. `_execute_pdf` now attempts this path first; the vision flows (board-localized and generic) are otherwise unmodified and run only as a fallback when deterministic extraction fails or is ambiguous.

**Generality caveat, stated plainly:** the row/column clustering is characterized against this fixture's clean, left-aligned layout (tolerance-based, not exact-coordinate — see `docs/specs/SPEC-M2P1-deterministic-document-extraction-v1.md` §3). This is **not** a general table-extraction capability. No claim is made that it works on an arbitrary drawing, a multi-page document, a scanned/OCR-required document, or a ruled-line table (`find_tables()` returns zero tables on this fixture — there are no ruled lines to detect in the first place). A different document layout needs its own characterization, following the same diagnose-then-implement pattern this milestone used, before this path can be trusted on it.

`tests/test_pdf_deterministic_extraction.py` covers all three boards × both fields (value, bbox, `extraction_method`, confidence above threshold, zero model calls — asserted via `FakeModelProvider`'s call log, not just by inspection), the `Panel-A` regression, content-driven ambiguity/miss handling, and the vision-fallback path. `tests/test_failure_path_evals.py`'s F12 scenario was rewritten (a scoped, authorized exception to the "no existing test may be modified" default) once its original question turned out to name both a real field and a real record unambiguously under the new deterministic-first ordering — the new architecture correctly answering it, not a regression; see the SPEC-M2P1 PR for the full trace.
