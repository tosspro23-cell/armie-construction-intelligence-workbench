# Review required

Institutional-knowledge gaps recorded here per SPEC-M1 §12. These are answered by the owner
over time; they are not inferred or resolved during implementation.

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
