# SPEC-M10 — Optional answer-wording polish pass

## Objective

Add an optional, final rewording pass over an already-computed, already-verified
deterministic answer, so the natural-language sentence a user reads sounds like a
knowledgeable colleague wrote it rather than a template-filled computation — without
that rewording ever becoming a second, unverified source of truth for what the
answer actually says.

## Rationale

Every answer this system produces today (`AgentService._natural_answer` and
the reconciliation/IFC/PDF result renderers it sits alongside) is deterministic
string assembly: a fixed template filled in with values that were independently
computed or independently verified earlier in the same request. That is exactly
right for `D-010`'s honest-disposition discipline — the answer never claims more
than the pipeline actually established — but it also means every answer reads like
what it is: `"这个项目中共有 **12** 扇窗。"`, not a sentence a person would say.

Raised directly by the project owner (2026-09-13), citing a prior project's
pattern: run one additional real model call, after every fact is already fixed,
whose only job is wording. The risk that pattern exists to guard against is
obvious and was named explicitly in the same request — a rewording pass must not
be allowed to become a backdoor around the deterministic pipeline's own honesty.
This spec's entire design is built around a single non-negotiable answer to that
risk: **a rewrite is only ever accepted if every number in it matches, exactly,
the set of numbers in the original deterministic answer.** Not "the model was told
not to change numbers" (a prompt is an instruction, not a guarantee) — an actual
code-level guard (`AgentService._polish_preserves_facts`) that discards any
rewrite failing this check and silently falls back to the original text.

**What this milestone is not.** It is not a synthesis step, not a second
extraction, and not a summarization of multiple sources — `_natural_answer`'s
existing fragments are still what determines what facts appear; the polish pass
receives that already-finished text and may only reword it. It does not run at
all for `clarification_required`/`unsupported`/`refused`/`error` dispositions —
those messages are deliberately precise (naming candidate documents, stating why
something failed) and a "make it sound nicer" rewrite risks paraphrasing away
exactly the specificity those messages exist for.

## Verified current-state assumptions

- `AgentService._finalize` (`apps/api/app/agent/graph.py`) is the single graph
  node that assembles the final `AgentResponse.answer_markdown` from
  `state["tool_result"]["answer"]`, for every execution path (IFC, PDF, multi-plan
  synthesis, reconciliation) — confirmed the only place `result["answer"]` becomes
  `answer_markdown`. This is the one place a polish pass can apply uniformly
  without duplicating logic per execution path.
- `app/schemas/models.py` already defines `AnswerSynthesis` (`answer_markdown: str
  = Field(min_length=1, max_length=2400)`), unused anywhere in the codebase today.
  Its docstring — "Natural-language rendering of verified facts only; no new
  claims." — already states this milestone's exact contract. Reused as this
  feature's structured-output response model rather than defining a near-duplicate
  type; not previously wired to anything, not referenced in
  `docs/decisions/REVIEW_REQUIRED.md` as a tracked gap.
- `ServiceContainer.text_provider_factory` / `get_text_provider` (`apps/api/app/
  providers/factory.py`) never returns `None` — it always resolves to a concrete
  `ModelProvider` (Ollama, Azure OpenAI, or OpenAI, based on `settings.llm_provider`)
  the same way the semantic planner already assumes without a null-check. No new
  provider-factory seam is needed; the existing one used for planning is reused.
- `ModelProvider.structured(purpose, response_model, prompt)` is the existing typed
  structured-output call every planning/verification model call in `graph.py`
  already uses (confirmed at `graph.py:310`, `:335`, `:378`, `:391`, `:454`, and
  the vision/verification call sites in `_execute_pdf`) — this milestone's call
  follows the exact same shape, not a new call pattern.
- `_audit`'s existing `step`/`event_type` classification (`apps/web/src/
  DecisionStory.tsx`'s `auditStage()`) has no case matching a step named
  `"polish_answer"`, so it falls into the default `"Final Response"` bucket —
  confirmed this places the new audit events under the Result step's own trace,
  which is where a user would look for "who last touched the wording of this
  answer," without any frontend classification change needed.

## Allowed scope

**A. Settings + provider call.**
- `Settings.enable_answer_polish: bool = False` (`apps/api/app/config.py`) —
  opt-in, matching every other optional-capability setting in this project
  (`azure_search_endpoint`, `rate_limit_*`). Off by default: no existing
  deployment or test changes behavior until this is explicitly turned on.
- `AgentService._polish_answer(state, answer) -> str`
  (`apps/api/app/agent/graph.py`): fetches the existing text provider, calls
  `provider.structured(purpose="answer_polish", response_model=AnswerSynthesis,
  prompt=...)` with an instruction that explicitly forbids adding, removing, or
  changing any number/date/name/identifier, and forbids introducing hedging or
  claims not already present. Any transport exception is caught and falls back to
  the original `answer` unchanged.

**B. The number-preservation guard (the actual enforcement, not the prompt).**
- `AgentService._polish_preserves_facts(original, rewritten) -> bool`: extracts
  every numeric token (`\d+(?:\.\d+)?`) from both strings and requires the two sets
  to be exactly equal — a rewrite that drops, adds, or alters any number is
  rejected outright, regardless of how plausible its prose reads. A rejected
  rewrite falls back to the original deterministic text, audited as
  `model_rejected`, not silently discarded.

**C. Wiring into `_finalize`, scoped to two dispositions only.**
- Applied only when `settings.enable_answer_polish` is true and the resolved
  disposition is `answered` or `partially_answered`. Every other disposition's
  message passes through unchanged, exactly as before this milestone.
- `execution_metadata` gains `answer_polished: bool` and `pre_polish_answer: str |
  None` (populated only when a rewrite was actually accepted), so the original
  deterministic text stays inspectable in the same response that shows the
  polished one — not just in the audit trail. `model_call_count` is incremented
  by exactly one when a rewrite is accepted (not attempted-and-rejected), so the
  existing "N model calls total" figure stays accurate.
- Every call — accepted, rejected, or failed — emits a paired `model_called`/
  (`model_completed` | `model_rejected` | `model_failed`) audit event carrying
  `actual_provider`/`actual_model`, matching the pairing discipline every other
  model-call site in `graph.py` already follows (the AI Search embedding call's
  prior lack of this exact pairing was an independent, already-fixed defect —
  see `docs/decisions/README.md` D-024 — not a precedent to repeat here).

**D. Frontend surfacing.**
- `apps/web/src/DecisionStory.tsx`'s Result step shows a collapsed-by-default
  note ("Answer was reworded by a model for tone — numbers unchanged") with the
  pre-polish deterministic text underneath, whenever `execution_metadata.
  answer_polished` is true — visible, not hidden inside the raw trace only.

**E. Tests, using the existing fake-provider seam (D-007), not live Azure.**
- The guard's own truth table: identical numbers in a different order/wording
  accepted; a dropped number rejected; an added number rejected; a reworded
  number (`12` → `12.0`) rejected (exact token match, not numeric equality —
  deliberately conservative).
- `_finalize` wiring: `enable_answer_polish=False` (default) never calls the
  polish path — asserted via the fake provider's own call log, not just output
  equality, per this repo's verification standard. `enable_answer_polish=True`
  with a fake provider returning a fact-preserving rewrite: `answer_markdown` is
  the rewrite, `execution_metadata.answer_polished` is true,
  `pre_polish_answer` is the original. A fake provider returning a
  fact-dropping rewrite: `answer_markdown` is the untouched original,
  `answer_polished` is false. A disposition of `clarification_required`: the
  polish path is never invoked regardless of the setting (call-log assertion).

## Explicitly excluded scope

- **Polishing `clarification_required`/`unsupported`/`refused`/`error` messages**
  — see Rationale. A future milestone could revisit this with its own guard
  design (e.g. preserving named document/field identifiers exactly, the way this
  milestone preserves numbers); not attempted here.
- **Numeric-equality-aware comparison** (accepting `12` rewritten as `12.0` or
  `12.00`) — deliberately conservative exact-token matching; a real formatting
  need can be revisited later against real failures, not guessed in advance.
- **Per-language or per-tone configuration of the polish prompt** — one fixed
  prompt, matching the answer's own `response_language`. Not configurable this
  milestone.
- **Caching or reusing a polished rewrite across repeated identical questions** —
  every accepted answered/partially_answered response pays one polish call; no
  memoization layer is added.
- **Deploying this with `enable_answer_polish=true` by default anywhere** — this
  spec adds the capability and its guardrails; turning it on for a given
  environment (including the live demo) is a separate, explicit deploy-config
  decision, tracked below.

## Affected surfaces

`apps/api/app/config.py` (new setting), `apps/api/app/agent/graph.py`
(`_polish_answer`, `_polish_preserves_facts`, `_finalize` wiring), `apps/api/app/
schemas/models.py` (reusing existing `AnswerSynthesis`, no schema change),
`apps/web/src/DecisionStory.tsx` (Result-step note), `tests/` (new focused test
file), `infra/bicep/apps.bicep` / `.github/workflows/azure-deploy.yml` (new
optional env var, opt-in-when-unset, mirroring the rate-limit precedent).

## Invariants

All prior invariants unchanged, including `D-010`'s honest-disposition contract:
disposition, verification status, and citations are computed entirely before this
pass ever runs and are never touched by it. New invariant this milestone adds:
**no code path may accept a polished rewrite that changes the set of numbers in
the original answer.** This is enforced in code (`_polish_preserves_facts`), not
only requested in the model prompt.

## Acceptance criteria

- The guard's full truth table (identical/dropped/added/reformatted numbers) is
  covered by a focused unit test and passes.
- With `enable_answer_polish=False` (the default), behavior is provably identical
  to pre-milestone `main` for every existing test — no existing test's assertions
  change.
- With `enable_answer_polish=True` and a fake provider, an accepted rewrite
  updates `answer_markdown`/`execution_metadata` exactly as specified in §C, and a
  rejected/failed one leaves the original answer completely unchanged — both
  proven via the fake provider's call log, not just final-value inspection (this
  repo's verification standard: a reproduction that doesn't rely on the same code
  path it's proving correct).
- `PYTHONPATH=apps/api python3 -m pytest -q` passes.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes.

## Documentation requirements

`docs/decisions/README.md` gains a `D-025` entry (this milestone's design summary
and the number-preservation guard's exact contract). `PROJECT_STATE.md` M10 entry.

## Git / stop conditions

One commit per subsection (A–E), spec committed alone first. Branch:
`feat/m10-answer-polish`. Stop and report rather than proceeding if: the
number-preservation guard cannot be made to reject a fabricated-number case in
testing (would mean the guard itself is unsound — do not ship a guard that
doesn't actually guard); or extending this to `clarification_required` messages
turns out to be needed to fully satisfy the original request (out of this spec's
scope — see Explicitly excluded scope — report back rather than silently
expanding scope).

## Owner decisions

- **OD-42 (owner-confirmed 2026-09-13, in conversation).** Build this now, at the
  cost of one additional real model call per answered/partially_answered
  response, rather than deferring it — the owner explicitly weighed the added
  latency/token cost against the UX improvement and chose to proceed, requesting
  the fallback/no-crash-when-unconfigured behavior in the same request.
- **OD-43 (owner-confirmed 2026-09-13, in conversation).** The new "simple house"
  demo IFC model that would fix the *underlying* reason doors/windows are hard to
  click in the BIM viewer (SPEC-M9's fixture walls have no real opening cut for
  them) is explicitly deferred — a front-end picking-priority mitigation
  (preferring the nearest non-wall hit within a wall-thickness margin) ships
  instead, tracked as its own `D-024` bug-fix entry, not this spec. Building a new
  fixture is left for a future, separately-scoped milestone.
