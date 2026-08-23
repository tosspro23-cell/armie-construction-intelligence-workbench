# SPEC-M1 — Test Seam, Reproducible Build & Deterministic Behavioural Contract

**Version:** v1 (frozen)
**Status:** Implementation-ready. All owner decisions are resolved (§13). No open blocking decisions remain.
**Authority:** This document is the authoritative M1 implementation contract. It supersedes all prior drafts and all chat-level discussion. Where this document and any conversation disagree, this document governs.
**Target repository:** `tosspro23-cell/armie-construction-intelligence-workbench`
**Base branch:** `chore/vendor-neutral-agent-handoff` @ `1936d3f456577c4527aa3f38ec15f2ce7aa2b1b1`
**Implementation branch:** `feat/m1-reliability-foundation`, branched from the base branch above — **not** from `main`
**Delegated to:** Claude Code (or approved coding agent)
**Canonical location:** `docs/specs/SPEC-M1-reliability-foundation-v1.md`. Commit this file as the **first** commit on the implementation branch, before any code change. No specification governs implementation from outside the repository.

### Identifier namespaces

Two decision namespaces exist in this repository and must not be conflated:

- `D-001` … `D-00n` — **architecture decision records** in `docs/decisions/`. `D-001`–`D-005` already exist; this milestone adds `D-006`–`D-008`.
- `OD-1` … `OD-8` — **owner decisions** scoped to this milestone, recorded in §13 of this document only. They are not ADRs and are not written to `docs/decisions/` under these identifiers.

---

## 1. Objective

Establish a **behavioural regression contract** and a **reproducible build** for the hybrid
deterministic/probabilistic pipeline, so later milestones can widen capability surface
without silently degrading planning safety, evidence, or verification.

This is not "add more tests." The repository currently has **no test seam for the
probabilistic path**: providers are constructed inline inside `AgentService` graph methods,
so no planner, repair, escalation, or failure path can be exercised without a live Ollama
daemon. Until that seam exists, every reliability claim about this system is unfalsifiable.

## 2. Business / architecture rationale

- **Business:** the product promise is *trusted* construction intelligence. Its
  differentiator is that it refuses, clarifies, and verifies rather than emitting a
  confident wrong number. Nothing automated currently protects that behaviour. A regression
  that converts a refusal into a plausible quantity is the most expensive failure mode this
  product has.
- **Architecture:** the deterministic/probabilistic boundary (`D-001`, `D-002`) is the core
  asset. Untested boundaries erode under future edits, especially with a 1,203-line
  orchestration module.
- **Portfolio:** testing a non-deterministic component deterministically is the specific
  artefact that distinguishes an AI systems engineer from a prompt integrator. Test count
  is not the signal.

## 3. Verified current-state assumptions

Reproduced against the base branch on 2026-08-23 (Python 3.12.3, Node 22.22.2, Linux).
Re-verify before starting; **stop and report** if any assumption is false.

| # | Assumption | Status |
|---|---|---|
| A1 | Base branch head `1936d3f` = `main` (`5eb9d75`) + 1 doc-only commit; fast-forwardable | verified |
| A2 | `PYTHONPATH=apps/api python3 -m pytest -q` → **2 passed** (0.46s) | verified |
| A3 | `npm install` in `apps/web` **fails** on clean checkout (`ERESOLVE`) | verified |
| A4 | `npm ci` in `apps/web` **also fails** (same conflict) | verified |
| A5 | Cause: `three@0.155.0` pinned; `web-ifc-three@^0.0.126` peer-requires `three@^0.149.0` | verified |
| A6 | `npm install --legacy-peer-deps && npm run build` passes (chunk-size warning only) | verified |
| A7 | Pinning `three@0.149.0` + `@types/three@^0.149.0` makes `npm install`, `npm ci`, `tsc -b`, and `vite build` **all pass**; bundle shrinks 619 kB → 585 kB | verified |
| A8 | **Neither `web-ifc` nor `web-ifc-three` is imported by any application source.** Their only consumers are `scripts/copy-ifc-wasm.mjs`, which copies assets already committed to Git. No runtime reference to `/wasm/`, `IFCWorker.js`, or any IFC loader exists in `src/`, `index.html`, or `vite.config.ts`. The viewer renders `THREE.BoxGeometry` from server-computed `/api/v1/project/viewer-elements`. | verified |
| A9 | `ModelProvider` declares `name` but **not** `model`; `graph.py` reads `provider.model` in ~15 places | verified |
| A10 | Providers acquired inline in graph methods (`graph.py:269`, `:810`, `:315`), not via `ServiceContainer` | verified |
| A11 | `graph.py:315` hardcodes escalation model `"qwen3:30b"` — absent from `config.py`, `.env.example`, and all docs | verified |
| A12 | `ruff check .` → 21 errors (10 `BLE001`, 7 `I001`, 3 `F401`, 1 `UP045`) | verified |
| A13 | `README.md:132` claims fixture tests cover "planning, IFC tools, document safety, verification, and provider parsing." Only two `IfcRepository` tests exist. **The claim is false.** | verified |
| A14 | `AuditStore` **persists** to a local JSONL file (`runtime/audit.jsonl`). Only conversation and request state are in-process. | verified |
| A15 | `main.py:85` enforces basename containment on the evidence endpoint. Path traversal is a **documentation omission, not a defect**. | verified |
| A16 | CJK special-casing is **extensive**: hardcoded Chinese phrase lists (`router.py:41`, `:63`), Chinese grouping keywords (`plan_validation.py:30`), a hardcoded Chinese clarification string (`graph.py:201`), and a full parallel Chinese answer-rendering branch with noun dictionaries (`graph.py:595–637`). | verified |
| A17 | `ResponseLanguage.code` is `Literal["en","zh-CN","pt-PT","fr","es"]`, but only English and Chinese rendering branches exist. `pt-PT`/`fr`/`es` silently fall through to English. | verified |
| A18 | `pyproject.toml` declares `requires-python = ">=3.9"`; only 3.12 verified | **unverified** on 3.9/3.10/3.11 — resolving this is in scope (§4.8) |

## 4. Allowed scope

### 4.1 Reproducible build (blocking prerequisite)

1. Pin `three@0.149.0` and a compatible `@types/three@^0.149.0` in `apps/web/package.json`.
2. Regenerate `package-lock.json` so plain `npm ci` succeeds with **no legacy flags**.
3. Verify `tsc -b` and the production `vite build` both pass after the pin.
4. Update `README.md`, `PROJECT_STATE.md`, and `AGENT_HANDOFF.md` to the command that
   actually works.
5. Record the conflict and resolution as `docs/decisions/D-006`.
6. **Do not** migrate the IFC/browser dependency stack in this milestone. **Do not** remove
   `web-ifc`, `web-ifc-three`, the copied WASM/worker assets, or the asset-preparation path
   (A8) — these are deferred to M1.5 and recorded in the REVIEW REQUIRED register (§12).

### 4.2 Provider test seam (blocking prerequisite)

7. Add `model: str` to the `ModelProvider` Protocol. Both concrete providers already
   satisfy it; this only makes an existing dependency honest.
8. Inject provider **factories** — not provider instances — through `ServiceContainer`,
   defaulting to the existing `get_text_provider` / `get_vision_provider`. Constraints,
   all acceptance-tested in §4.7:
   - provider **selection logic stays centralized in `providers/factory.py`**; the seam
     changes *who calls* the factory, never *how it chooses*;
   - `llm_provider` handling (`ollama` / `hybrid` / `openai`) is unchanged;
   - `model_call_timeout_seconds` and `request_timeout_seconds` still reach the provider;
   - bounded-repair and escalation ordering is unchanged;
   - audit fields `actual_provider`, `actual_model`, `retry_count`, `planning_mode`,
     `configured_provider`, `provider_fallback_reason` are unchanged;
   - the cancellation and deadline boundary in `main.py` still wraps the same invocation.
9. The escalation provider at `graph.py:315` resolves through the same seam. Escalation
   becomes **configurable and opt-in**: add an optional escalation-model setting to
   `Settings`, defaulting to disabled, documented in `.env.example` and `PROJECT_STATE.md`.
   Bounded escalation behaviour is preserved when configured. No developer or CI
   environment may be silently required to hold a 30B model.
10. Add `tests/fakes/fake_provider.py`: `FakeModelProvider` implementing `ModelProvider`
    with **both `name` and `model`**, a scripted response queue keyed by `purpose`, a call
    log, and the ability to raise `StructuredOutputError`, raise transport errors, sleep
    past a deadline, or return schema-valid-but-semantically-wrong payloads. No network
    I/O; usable with no Ollama installed.

### 4.3 Canonical disposition contract

11. The following mapping is **normative**. Every test in §4.5 asserts against it, and any
    implementation divergence is a defect finding, not a test to adjust:

| Condition | Required disposition |
|---|---|
| Provider transport failure, timeout, or unavailability | `error` |
| Model output unparseable after bounded repair | `error` |
| Plan valid but capability unsupported | `unsupported` / refusal, with rationale |
| Request ambiguous, or validation cannot establish a safe plan | `clarification` |
| Verifier fails | not `answered`; no numeric claim presented as verified |
| Client cancellation | `cancelled`, **and no conversation-context mutation** |
| Request deadline exceeded | timeout state, distinct from `error` and `unsupported` |

12. In no branch may a numeric or factual claim be emitted without evidence, citations, and
    a passing `VerificationStatus`.

### 4.4 Deterministic contract tests (pure functions, no provider)

13. `tests/test_router_contract.py` — `fast_path_coverage`, `heuristic_plan`,
    `heuristic_multi_plan`, `capability_gate`, `selected_element_plan`,
    `ground_plan_to_selection`, `cross_source_join_requested`, `nearest_space_requested`.
    Must include ≥1 case per supported entity alias; a cross-source join refused rather
    than downgraded; a deictic follow-up grounded to a selected element.
14. `tests/test_plan_validation_contract.py` — `canonicalize_subplan`,
    `canonicalize_multi_plan`, `validate_multi_plan`, `calibrate_interpretation_confidence`,
    `enforce_grouped_request_contract`, `eligible_scalar_count_batch`,
    `actual_result_shape`, `verify_execution_consistency`. Every correction path asserts
    both the corrected plan **and** the emitted correction record.
15. `tests/test_verification_contract.py` — `InvariantValidator` (answered-without-evidence
    and answered-without-citations both fail), `DeterministicVerifier` (agreement and
    injected-disagreement, asserting `corrected_value` is populated), `EvidenceVerifier`
    (empty / below-threshold / at-threshold), `verification_status`.

### 4.5 Failure-path evals (F1–F12, using the fake provider)

Each asserts the disposition from §4.3 **and** the absence of an unverified numeric claim.
"No exception raised" is not an assertion.

| ID | Case | Required outcome |
|---|---|---|
| F1 | Malformed / non-JSON model output → bounded repair succeeds | `answered`, repair recorded in audit |
| F2 | Malformed output → repair also fails | `error`; no numeric claim |
| F3 | Fenced-JSON and empty-string outputs | handled by `_extract_json` or → `error`; never silently coerced |
| F4 | Schema-valid but semantically wrong plan (`operation` contradicts `expected_result_shape`) | caught by `validate_multi_plan`; repair entered |
| F5 | Persistent validation failure → escalation attempted → escalation unavailable | `clarification`; escalation attempt and failure both present in audit |
| F6 | Provider raises transport error | `error`, distinct from `unsupported` and `clarification` |
| F7 | Provider exceeds `model_call_timeout_seconds` | timeout state, distinct from `error` |
| F8 | Valid-but-unsupported capability | refusal with rationale; **no tool execution occurs** |
| F9 | Cross-source join requested | rejected **before** tool execution; assert the IFC/PDF tool was never called |
| F10 | Client cancellation mid-invocation | `cancelled`, **and conversation context is unmutated** |
| F11 | Tool returns a shape mismatching the plan's `expected_result_shape` | `verify_execution_consistency` raises the issue; not `answered` |
| F12 | Vision/PDF path fails or returns low-confidence evidence | clarification or refusal; **no fabricated field value**, no invented citation |

16. Escalation-path audit behaviour (F5) must assert `actual_model` reflects the escalation
    model and `retry_count == 2`, so the opt-in escalation change stays regression-protected.

### 4.6 Characterization tests (pin current behaviour, do not fix)

17. `tests/test_language_selection_characterization.py` — pin the *current* behaviour of
    the full CJK surface identified in A16: the codepoint scans (`router.py:169`,
    `graph.py:262`), the Chinese phrase and grouping keyword tables, the hardcoded Chinese
    clarification string, and at least one Chinese answer-rendering branch each for count,
    group-by, and properties.
18. Add one test pinning A17: a plan with `response_language="pt-PT"` currently renders via
    the English branch. Module docstring must state these tests pin existing behaviour
    ahead of a future language-handling decision and are **not** an endorsement of it.
    M1 must preserve and test current behaviour only; multilingual handling must not be
    redesigned, narrowed, or extended in this milestone.

### 4.7 Seam-invariance tests

19. `tests/test_provider_seam_invariance.py` — assert that for each `llm_provider` value
    (`ollama`, `hybrid`, `openai`), the container-resolved factory returns a provider with
    the same `name`, `model`, and timeout as the pre-refactor `get_text_provider` /
    `get_vision_provider` would have. This is the testable replacement for any
    unfalsifiable "live-provider behaviour unchanged" claim.
20. Assert the audit-field set emitted for one scripted end-to-end run matches a committed
    snapshot of keys (keys and provider/model/retry values, not free-text messages).

### 4.8 CI

21. `.github/workflows/ci.yml`, on push and pull_request:
    - **backend**: Python matrix **3.9, 3.10, 3.11, 3.12** → `pip install -e 'apps/api[dev]'`
      → `PYTHONPATH=apps/api python3 -m pytest -q`.
    - **frontend**: Node 22 → `npm ci` (**no flags**) → `npm run build`.
    - **lint**: `ruff check` with correctness and import rules blocking (`F`, `E9`, `I`,
      `F401`). `BLE001` stays **advisory** — those blind-excepts sit on the failure paths
      this milestone characterizes; fixing them before F1–F12 exist inverts the order.
    - No Ollama, no model downloads, no secrets, no network egress beyond package
      registries. Any test requiring a live model is out of scope for this milestone.
22. The matrix exists to **test** the declared `requires-python = ">=3.9"`, not to assume
    it. If any version in the matrix fails: **stop, report the exact incompatibility, and
    raise an explicit owner decision before narrowing the supported baseline.** Do not
    silently modify `requires-python`, and do not silently drop a matrix entry.
    `ifcopenshell>=0.8` and the `eval-type-backport` union-syntax path are the likely
    failure points.
23. **Mutation verification is manual and must not run in CI.** Provide
    `docs/specs/M1-mutation-protocol.md` describing how to disable a guard and observe the
    corresponding test fail. Results go in the PR body as evidence; no mutation tooling,
    job, or dependency enters the normal pipeline.

### 4.9 Documentation truthfulness

24. `README.md:132` — replace the false coverage claim (A13) with the actual scope.
25. `PROJECT_STATE.md` — actual test count and commands; CI-verified verification block;
    the opt-in escalation model setting; the working frontend install command; correct A14
    (audit JSONL persists; conversation/request state is in-process) and A15 (basename
    containment is enforced; the gap is documentation).
26. `AGENT_HANDOFF.md` — updated code map and start-here commands.
27. `CLAUDE.md` — updated canonical check commands if they change.
28. `docs/decisions/` — add `D-006` (dependency resolution), `D-007` (provider injection
    seam and deterministic fake as the probabilistic test boundary), `D-008` (canonical
    disposition contract, §4.3).

## 5. Explicitly excluded scope

Decomposing or restructuring `graph.py` (deferred to M1.5 — tests before refactor);
removing or replacing CJK handling (§4.6 pins only); removing the dead `web-ifc` /
`web-ifc-three` dependencies and their committed assets (deferred to M1.5, §12); migrating
the IFC/browser dependency stack; any HTTP-level eval corpus or scored harness (M2);
vision/PDF fakes beyond F12; cross-source joins; multilingual redesign; multi-project
workspace; arbitrary IFC query language; auth, tenancy, rate limiting, persistence,
migrations; full OpenAI parity; UI redesign; autonomous viewer reasoning; broadening CORS,
secrets, or network egress; new `apps/api` runtime dependencies beyond `[dev]` tooling; any
non-synthetic data; merging to `main`.

## 6. Affected architecture surfaces

`apps/api/app/providers/base.py`, `apps/api/app/config.py` (escalation setting only),
`apps/api/app/services.py`, `apps/api/app/agent/graph.py` (call sites only),
`apps/web/package.json`, `apps/web/package-lock.json`, `tests/`, `.github/workflows/`,
`docs/decisions/`, `docs/specs/`, `.env.example`, `PROJECT_STATE.md`, `AGENT_HANDOFF.md`,
`README.md`, `CLAUDE.md`.

## 7. Invariants that must remain true

1. Python/IfcOpenShell remains authoritative for IFC counts, grouping, arithmetic (`D-001`).
2. Every executable plan still passes canonicalization, cross-field validation, and the
   capability gate before any tool runs (`D-002`).
3. `answered` still carries evidence, citations, and a `VerificationStatus` (`D-003`).
4. The §4.3 dispositions remain distinct and are never collapsed (`D-004`).
5. Public fixtures remain synthetic (`D-005`).
6. Audit event names, stage names, and payload keys are unchanged.
7. Provider selection semantics are unchanged (§4.2, tested by §4.7).

## 8. Implementation requirements

- Tests live under the existing top-level `tests/`, run under the existing `pytest` config.
- The fake provider is the **only** mechanism for driving probabilistic paths. Do not
  monkeypatch `app.providers.factory` globals as a substitute for the injection seam — that
  leaves production wiring untested and reproduces the gap this milestone exists to close.
- Tests must not bypass model selection, hybrid routing, timeout settings, repair calls, or
  escalation ordering. They substitute the provider, not the pipeline.
- No test requires network access, an Ollama daemon, an OpenAI key, or a downloaded model.
- Suite runtime target: **under 30 seconds** in CI.
- Do not lower `route_confidence_threshold`, `pdf_confidence_threshold`, or
  `max_verification_retries` to make a test pass. Report the blockage as a finding.

## 9. Acceptance criteria

1. On a fresh clone, `npm ci` and `npm run build` succeed with **no flags**.
2. `PYTHONPATH=apps/api python3 -m pytest -q` passes locally and in CI.
3. CI green on the implementation branch across the full Python matrix, or a §4.8/22 stop
   report filed; run URL in the PR body.
4. F1–F12 all implemented; §4.4 ≥ 24 assertions across ≥ 12 test functions; §4.6 ≥ 4
   characterization tests; §4.7 seam-invariance tests pass.
5. The two existing tests in `tests/test_public_workspace.py` pass **unmodified**.
6. Manual mutation evidence for **at least three** of F2, F5, F8, F9, F10, F11 — observed
   failure output included in the PR body, produced per §4.8/23, with nothing added to CI.
7. Provider semantics preserved, evidenced by §4.7 rather than by any live-provider claim.
   The PR must state explicitly: *"Live-provider end-to-end behaviour was not revalidated in
   this milestone; equivalence is asserted at the factory-selection and audit-field level
   only."* Do not assert unverified runtime equivalence.
8. Every documentation claim changed in §4.9 corresponds to a command actually run.
9. `git diff --stat` shows no changes outside §6.
10. This specification is committed at `docs/specs/SPEC-M1-reliability-foundation-v1.md` in
    the first commit on the branch.

## 10. Documentation / write-back requirements

Repository-native only. No architectural or project-state conclusion may exist solely in
chat or in a PR comment. `PROJECT_STATE.md` must be accurate as of the merge commit.

## 11. Git / branch expectations & stop conditions

Branch from `chore/vendor-neutral-agent-handoff`. One branch, one PR, opened against `main`.
Do not merge. Do not force-push shared branches. Do not modify `main`. `main` remains the
stable public release until implementation and owner review are complete.

**Stop and report if:**

- any §3 assumption is false;
- any Python version in the §4.8 matrix fails;
- the seam cannot be added without altering audit payloads, selection logic, timeout
  propagation, or fallback/escalation ordering;
- the `three@0.149.0` pin breaks the viewer build or `tsc -b`;
- an existing test must be modified to pass;
- a failure-path test shows the system emitting a numeric or factual claim where it should
  refuse — a **defect finding**; report it, do not adjust the test;
- scope begins expanding into `graph.py` restructuring or any deferred M1.5 work.

## 12. REVIEW REQUIRED register

Record in `docs/decisions/REVIEW_REQUIRED.md`; do not infer answers during implementation.

- Why `qwen3:30b` was selected for escalation, and whether it was ever expected to be installed.
- Whether CJK handling is a committed product requirement or temporary compatibility scaffolding.
- Why `three@0.155.0` was chosen despite the `web-ifc-three` peer range.
- **M1.5 candidate:** `web-ifc`, `web-ifc-three`, the copied WASM/worker assets, and the
  redundant asset-preparation path (A8). After M1 establishes the regression net, determine
  whether these were retained for an intended client-side IFC parsing path or are genuinely
  removable dead weight. M1 must not remove them.
- Why `ResponseLanguage` admits `pt-PT`, `fr`, `es` with no rendering branch (A17). A later
  milestone must decide explicitly whether to narrow the language contract, implement the
  declared languages, or redesign language handling.
- Whether OpenAI/hybrid support is intended for active release or future experimentation.

## 13. Owner decisions — all resolved

| ID | Decision | Resolution |
|---|---|---|
| **OD-1** | Frontend dependency resolution | **Option (a) approved.** Pin `three@0.149.0` and compatible `@types/three`; regenerate the lockfile; require plain `npm ci` with no legacy flags; verify `tsc -b` and the production build. Do not migrate the IFC/browser dependency stack in M1. Verified end to end (A7): bundle 619 → 585 kB, viewer uses only core `three` APIs unchanged across 0.149–0.155. |
| **OD-2** | `qwen3:30b` escalation | **Configurable and opt-in.** Optional escalation-model setting, default disabled, documented in `.env.example` and `PROJECT_STATE.md`. Bounded escalation behaviour preserved when configured. No environment silently requires a 30B model. |
| **OD-3** | Ruff in CI | **Selectively blocking.** `F`, `E9`, `I`, `F401` blocking; `BLE001` advisory until F1–F12 exist. |
| **OD-4** | Python support | **3.9–3.12 CI matrix approved.** The repository claims `>=3.9`, so M1 tests that claim rather than silently narrowing it. Any failure → stop, report the exact incompatibility, raise an explicit owner decision. Do not silently modify `requires-python`. |
| **OD-5** | Git governance | **Do not** fast-forward the vendor-neutral handoff branch into `main` before M1. Branch M1 from `chore/vendor-neutral-agent-handoff`. `main` remains the stable public release. Nothing merges automatically. |
| **OD-6** | Merge authority | Owner-only. No agent may represent a merge as performed. |
| **OD-7** | Dead frontend dependencies | **Deferred from M1** to M1.5 / REVIEW REQUIRED (§12). M1 must not remove them. |
| **OD-8** | `ResponseLanguage` surface | **Characterize only in M1** (§4.6/18). Preserve and test current behaviour. Do not redesign multilingual handling. A later milestone decides whether to narrow, implement, or redesign. |

No open owner decisions remain. Remaining unknowns are institutional-knowledge gaps
recorded in §12, which are answered by the owner over time and never inferred during
implementation.
