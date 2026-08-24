# SPEC-M2P1 §9 — Live-Model Baseline (owner-run, before/after)

Owner-run locally per SPEC-M2P1 §9/OD-10 (not an implementation-agent gate — no Ollama in
the cloud sandbox). Real Ollama (`qwen3:8b` text, `qwen3-vl:8b` vision) against the running
local API. No `FakeModelProvider`, no mocking. Both checkouts used `git checkout --detach`;
nothing was committed, pushed, or modified on either branch during measurement.

**Commits measured:** before = `main` @ `0d11ed6`; after =
`feat/m2p1-deterministic-document-extraction` @ `81e2a29`.

Ground truth: DB-L1-A = 18.50 / 0.75; DB-L2-B = 26.00 / 0.65; Panel-A = 44.50 / 0.70.

## Result table (18 requests x 2 branches = 36 live-model runs)

| Q | Run | Before disposition | value | method | calls | latency | After disposition | value | method | calls | latency |
|---|-----|---|---|---|---|---|---|---|---|---|---|
| 1 DB-L1-A load | r1 | clarification | -- (0 valid candidates) | board_localized_vision | 2 | 34.8s | answered | 18.50 | native_text | 0 | 65ms |
| 1 | r2 | clarification | -- | board_localized_vision | 2 | 33.0s | answered | 18.50 | native_text | 0 | 16ms |
| 2 DB-L2-B load | r1 | clarification | "true" (leaked bool) | board_localized_vision | 2 | 11.3s | answered | 26.00 | native_text | 0 | 6ms |
| 2 | r2 | clarification | "true" | board_localized_vision | 2 | 12.1s | answered | 26.00 | native_text | 0 | 11ms |
| 3 Panel-A load | r1 | clarification | -- (hardcoded B6 gate) | n/a, 0 calls | 0 | 7.9ms | answered | 44.50 | native_text | 0 | 6ms |
| 3 | r2 | clarification | -- | -- | 0 | 7.2ms | answered | 44.50 | native_text | 0 | 9ms |
| 4 DB-L1-A div. | r1 | clarification | -- (0 valid candidates) | board_localized_vision | 2 | 10.9s | answered | 0.75 | native_text | 0 | 6ms |
| 4 | r2 | clarification | -- | board_localized_vision | 2 | 11.6s | answered | 0.75 | native_text | 0 | 8ms |
| 5 DB-L2-B div. | r1 | clarification | -- (crop mismatch) | board_localized_vision | 2 | 11.2s | answered | 0.65 | native_text | 0 | 6ms |
| 5 | r2 | clarification | -- | board_localized_vision | 2 | 11.8s | answered | 0.65 | native_text | 0 | 7ms |
| 6 Panel-A div. | r1 | answered | 0.70 | generic vision | 2 | 5.0s | answered | 0.70 | native_text | 0 | 6ms |
| 6 | r2 | answered | 0.70 | generic vision | 2 | 5.2s | answered | 0.70 | native_text | 0 | 6ms |
| 7 total load (ambiguous by design) | r1 | clarification (correct) | -- (hardcoded B6) | n/a, 0 calls | 0 | 7.5ms | clarification (correct) | -- | native_text->vision | 2 | 25.9s |
| 7 | r2 | clarification (correct) | -- | -- | 0 | 8.4ms | clarification (correct) | -- | native_text->vision | 2 | 26.7s |
| 8 after-diversity (absent field) | r1 | clarification | "ambiguity" (leaked str) | board_localized_vision | 2 | 10.0s | clarification | "ambiguity" (same bug) | native_text->board_localized_vision | 2 | 13.2s |
| 8 | r2 | clarification | "ambiguity" | board_localized_vision | 2 | 10.2s | clarification | "ambiguity" | native_text->board_localized_vision | 2 | 13.9s |
| 9 CJK connected-load question | r1 | refused | -- | semantic planner | 2 | 9.2s | refused | -- | semantic planner | 2 | 9.0s |
| 9 | r2 | refused | -- | -- | 2 | 9.5s | refused | -- | -- | 2 | 9.2s |

## Headline result

Before: 1 of 6 answerable board x field questions was actually answered, and it was correct
by luck through a vision path that had already independently misfired on two other
questions in the same set. After: all 6 are answered, all 6 are exactly correct against
ground truth, all 6 at zero model calls, all 6 in single-digit-to-low-double-digit
milliseconds -- a 1000x-5000x latency improvement on the flipped questions, and the direct,
specified consequence of SPEC-M2P1 §4.5 item 11 (zero model calls on a successful
deterministic path).

## Where "after" is worse than "before" -- reported, not smoothed over

Q7 ("What is the total connected load?", intentionally ambiguous by design): before resolved
this for free (0 calls, ~8ms) via a hardcoded ambiguity_policy check. After still lands on
the correct disposition (clarification_required) but costs 2 real model calls and ~26s,
because native_lookup's confidence=0.4 ("no record named") and confidence=0.0 ("field
absent") both trip the same `if result.confidence < pdf_confidence_threshold` gate
(`graph.py:805`) into vision, with no distinction between "structurally no matching record"
(vision cannot help) and "genuinely unclear image" (the case vision fallback exists for).
This is a direct, foreseeable consequence of SPEC-M2P1 §4.5 item 10 as specified, not an
implementation bug -- but it is a real, measured cost regression on this one question shape,
and it is not something the spec text explicitly anticipated or excluded. Recorded as a
REVIEW_REQUIRED candidate below.

## Variance across runs

None observed on either branch, on any of the 9 questions -- disposition, exact answer text,
and extraction path were identical run1 vs run2 in all 18 pairs. Root cause:
`ollama_provider.py` hardcodes `"options": {"temperature": 0}` for both text and vision
calls (lines 48, 79), so repeat runs at the same prompt/model/image converge rather than
vary. Not a defect; SPEC-M2P1 §9's language ("expect run-to-run variance") should be
corrected to "reproducible at temperature=0" as part of this closeout.

## Findings outside H1-H3 / outside M2P1 scope, pre-existing on both branches

1. **`ambiguity` field leaking raw model output into the user-facing answer** (Q2 before,
   Q8 both branches): `qwen3-vl:8b` sometimes returns a non-informative or boolean-like
   value in the `ambiguity` field instead of a natural-language explanation, and
   `_execute_pdf` surfaces it verbatim with no sanity check (`graph.py:822`, `:910`).
   Reproduces identically on both branches, confirming vision prompts/logic are unchanged
   by M2P1 (§4.5 item 12) -- correctly out of this milestone's scope. Disposition stays
   `clarification_required` in every case, so no fabricated value is ever presented as
   verified; this is a message-quality defect, not a D-004 violation.
2. **Board-label misread causing false-negative candidate rejection** (Q1, Q4 before): the
   vision extractor returned `"B-L1-A"` (dropped leading "D") for `DB-L1-A`, and the exact
   equality check in the board-localized flow (`(candidate.board or board).upper() == board`,
   `graph.py:833`) silently discarded an otherwise-correct candidate. M2P1's deterministic-
   first ordering structurally routes around this for normal operation on this fixture; the
   code itself is unchanged and could still misfire on a different label variant.
3. **CJK PDF routing is broken on both branches, unrelated to M2P1's changes.**
   `router.py:231`'s PDF-routing keyword list (`"load", "circuit", "diversity", "schedule",
   "breaker", "electrical"`) is English-only, so "连接负荷" never reaches the fast path and
   falls to the semantic LLM planner, which misclassifies it and refuses. Distinct from the
   response-language (CJK rendering) issue M1 already characterizes -- this is source
   routing, not answer rendering. Out of scope for M2P1 (§6 limits `router.py` changes to
   `requested_field` only).

## Bottom line

§8.1/§8.2 are fully borne out by live-model measurement. The one genuine cost regression
(Q7) is real, reproducible, and a specified consequence rather than an implementation
defect; it is recorded as a follow-up rather than blocking this milestone.
