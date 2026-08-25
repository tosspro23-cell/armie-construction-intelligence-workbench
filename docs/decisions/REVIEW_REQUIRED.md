# Review required

Institutional-knowledge gaps recorded here per SPEC-M1 §12. These are answered by the owner
over time; they are not inferred or resolved during implementation.

## RESOLVED by M1.5: disposition collapse / `_refuse` unreachability

Previously (through M2P1): `error`, `clarification`, and `unsupported` all surfaced as
disposition `refused` on the semantic-planning failure path, a partial violation of the
D-004 honest-failure invariant. SPEC-M1.5 (`docs/specs/SPEC-M1.5-disposition-contract-v1.md`
§4A, D-010) fixes this: `AgentService._unsupported_subresult` now maps the actual underlying
cause of a `source="unsupported"` subplan to the correct disposition (`error` /
`clarification_required` / `unsupported`) instead of hardcoding `refused`, and `_refuse`'s
own historical `refused` fallback is corrected to `unsupported` for the same reason. See
D-010 for the full mechanism and `tests/test_disposition_contract.py` for the regression
coverage.

## RESOLVED by M1.5: `heuristic_multi_plan` cross-source-join crash

`apps/api/app/agent/router.py`'s `heuristic_multi_plan` constructed
`MultiQueryPlan(subplans=[], ...)` in its own cross-source-join refusal branch, which
violated the schema's `min_length=1` and raised a `pydantic` `ValidationError` instead of
returning gracefully. Fixed in SPEC-M1.5 §4C to construct a valid single
`source="unsupported"` subplan, matching `heuristic_plan`'s own single-plan pattern. The
underlying duplication (this check, `heuristic_plan`'s copy, and
`_synthesize_multi_response`'s copy, alongside `AgentService._resolve_context`'s — the one
live, authoritative check) is mapped and documented inline at each call site rather than
removed outright, since `heuristic_plan` and `heuristic_multi_plan` are directly unit-tested
independent of the graph. `tests/test_disposition_contract.py` proves the reachability
finding directly rather than leaving it as an assertion; the former crash-pinning test is
rewritten to assert the fixed, graceful behaviour
(`test_heuristic_multi_plan_gracefully_refuses_cross_source_join`).

## Why `qwen3:30b` was selected for escalation, and whether it was ever expected to be installed

`graph.py` hardcoded `"qwen3:30b"` as the semantic-repair escalation model, absent from
`config.py`, `.env.example`, and all prior documentation (A11). SPEC-M1 makes this
configurable and opt-in via `Settings.ollama_escalation_model` (default disabled, OD-2), but
does not answer why `qwen3:30b` specifically was chosen, or whether any environment was ever
expected to hold it.

## Whether CJK handling is a committed product requirement or temporary compatibility scaffolding

`router.py`, `plan_validation.py`, and `graph.py` carry extensive hardcoded Chinese phrase
lists, grouping-keyword tables, a hardcoded clarification string, and a full parallel
Chinese answer-rendering branch (A16). SPEC-M1 M1 characterizes and pins this behaviour only
(§4.6); it does not decide whether this is a committed, maintained product surface or
scaffolding that should be redesigned, generalized, or removed.

## Why `three@0.155.0` was chosen despite the `web-ifc-three` peer range

The frontend previously pinned `three@0.155.0` while `web-ifc-three@^0.0.126` peer-requires
`three@^0.149.0`, making a clean `npm install`/`npm ci` fail (A3-A5). SPEC-M1 M1 resolves the
conflict by pinning `three@0.149.0` (D-006, OD-1), but does not establish why `0.155.0` was
chosen originally.

## M1.5 candidate: `web-ifc`, `web-ifc-three`, and the redundant asset-preparation path

Verified (A8): neither `web-ifc` nor `web-ifc-three` is imported by any application source.
Their only consumers are `scripts/copy-ifc-wasm.mjs`, which copies assets already committed
to Git; the viewer renders `THREE.BoxGeometry` from the server-computed
`/api/v1/project/viewer-elements` endpoint, not client-side IFC parsing. SPEC-M1 M1 must not
remove them (OD-7). After M1 establishes the regression net, a future milestone should
determine whether these were retained for an intended client-side IFC parsing path or are
genuinely removable dead weight.

## Why `ResponseLanguage` admits `pt-PT`, `fr`, `es` with no rendering branch

`ResponseLanguage.code` is `Literal["en", "zh-CN", "pt-PT", "fr", "es"]`, but only English and
Chinese rendering branches exist in `AgentService._natural_answer`; the other three silently
fall through to the English branch (A17). SPEC-M1 M1 characterizes this behaviour only
(§4.6/18, OD-8); a later milestone must decide explicitly whether to narrow the language
contract, implement the declared languages, or redesign language handling.

## Whether OpenAI/hybrid support is intended for active release or future experimentation

`llm_provider` accepts `ollama`, `hybrid`, and `openai`, and `providers/factory.py` selects
between `OllamaProvider` and `OpenAIProvider` accordingly, but the public release documentation
states OpenAI has not been validated end to end. SPEC-M1 M1 preserves this selection logic
unchanged and asserts its equivalence only at the factory-selection/audit-field level
(`test_provider_seam_invariance.py`); it does not decide whether OpenAI/hybrid is an active
release target.

## RESOLVED by M1.5: vision fallback did not distinguish "no matching record" from "genuinely unclear image"

Previously (M2P1): a structurally unanswerable question (no record named at all) and a
genuinely unclear image tripped the same `if result.confidence < pdf_confidence_threshold`
gate into the vision fallback, because `native_lookup`'s confidence=0.4 ("no record named")
carried no distinction from "vision might actually help here." Measured live: the
intentionally-ambiguous "total connected load" question cost 2 real model calls and ~26s
post-M2P1, versus 0 calls pre-M2P1. Fixed in SPEC-M1.5 §4B/OD-14 (D-010):
`DocumentQueryResult.miss_reason` carries this distinction forward, and `_execute_pdf`
routes a structural record-miss straight to `clarification_required` at zero model calls.
Every other below-threshold case (field absent, multiple field or record matches) is
unchanged. Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md` (the original
finding) and `tests/test_disposition_contract.py`'s Q7 regression (the fix, asserting
`fake.calls == []`).

## Pre-existing: the vision path's `ambiguity` field is surfaced to the user with no validation

`graph.py:822` and `:910` pass `location.ambiguity`/`visual_verification.rationale`
verbatim into the user-facing answer. Live-model testing observed `qwen3-vl:8b` sometimes
returning a non-informative or boolean-like value in this field (observed: `"true"`,
`"ambiguity"`) instead of a natural-language explanation. Reproduces identically on both
`main` @ `0d11ed6` and the M2P1 branch, confirming the vision prompts/logic are genuinely
unmodified by M2P1 (§4.5 item 12) -- pre-existing, not introduced or fixed by this milestone.
No fabricated value is ever presented as verified (disposition stays
`clarification_required` in every observed case), so this is a message-quality defect, not
a D-004 violation. Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md`.

## M2: `scripts/generate_demo_data.py` no longer reproduces the committed fixtures byte-for-byte

SPEC-M2's fixture work (`Tag` population on the 8 door/window instances, OD-18; a second
schedule page, §4E.2) was applied directly to the committed `demo_data/armie_demo.ifc` and
`demo_data/armie_demo_schedule.pdf`, not to `scripts/generate_demo_data.py`. This was a
deliberate scope decision, not an oversight: the script is absent from SPEC-M2 §6's affected
surfaces, no §8 acceptance criterion requires touching it, and running `make_ifc()` would
regenerate every `GlobalId` from scratch (verified: `ifcopenshell.api.run("root.create_entity",
...)` assigns a fresh random GUID per run) and silently destroy the Tag edit's fixture identity.
Re-running the script today would produce a fixture where `Tag` is unset again and the PDF has
only one page — a real, live drift between the generator and the committed fixtures. A future
milestone should either fold the `Tag` values and the second schedule page into `make_ifc()`/
`make_pdf()` directly, or explicitly document the two-step process (generate, then apply the
committed fixture edits) as the intended workflow.

## Pre-existing: PDF-question routing (`router.py:231`) is English-only

The keyword list gating the PDF-domain heuristic fast path (`"load", "circuit", "diversity",
"schedule", "breaker", "electrical"`) is English-only, so a CJK document question (e.g.
"连接负荷是多少" for "what is the connected load") never reaches it and falls to the
semantic LLM planner, which live-model testing observed misclassifying and refusing it on
both `main` @ `0d11ed6` and the M2P1 branch. Distinct from the response-language (CJK
answer-rendering) behaviour M1 already characterizes (§4.6) -- this is source-routing
detection, not answer language, and was not touched by M2P1 (§6 limits `router.py` changes
to `requested_field` only). Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md`.
