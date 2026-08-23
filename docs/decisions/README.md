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
