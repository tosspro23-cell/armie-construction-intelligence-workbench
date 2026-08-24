# SPEC-M1.5: Disposition Contract & Cross-Source-Join Guard Consolidation — v1

## 1. Objective

Replace the collapsed disposition handling in the response layer with an explicit,
expressive contract distinguishing `error`, `clarification_required`, `unsupported`, and
`refused` as separate terminal states (fixing the documented D-004 violation), and
consolidate the duplicated, drifting cross-source-join guard implementations that trace
back to the same code region — including the one that currently crashes.

## 2. Rationale

M1 documented that `error`/`clarification`/`unsupported` all collapse to `refused` today.
The M2P1 live-model baseline (Q7) independently surfaced a structurally identical instance
of the same disease in the PDF fallback path: a structural record-miss (confidence 0.0) and
a genuinely ambiguous visual match (confidence 0.4) share one threshold gate with no reason
carried forward, causing a needless ~26s/2-call vision round-trip on a question the
deterministic layer had already correctly triaged.

M2 (cross-source reconciliation) will need to express compound results ("3 of 4 fixtures
matched, 1 dimension mismatch") — this requires a disposition contract richer than binary
answered/refused. This is the last point at which the foundation can be fixed before M2
builds on top of it, per the original M1.5 sequencing rationale.

A related, previously-documented low-priority latent defect (`heuristic_multi_plan`'s
`MultiQueryPlan(subplans=[])` schema violation) traces back to the exact same code region
(cross-source-join handling across `graph.py`/`router.py`) and is folded in per the existing
`REVIEW_REQUIRED.md` recommendation, rather than deferred a second time.

## 3. Verified current-state assumptions

All verified directly against `main` @ `8ef76a9` (post-M2P1), not assumed from prior
documentation:

- `graph.py:571` (`_unsupported_subresult`) hardcodes `disposition="refused"` for any
  subplan with `source="unsupported"`, regardless of whether the cause was a genuinely
  unsupported capability, exhausted repair, exhausted escalation, or a raw transport error.
- `graph.py:1048` (`_refuse`) *already* distinguishes `"error"` vs `"refused"` via
  `state.get("planner_error")` — but this branch is dead code today: `_refuse` is reachable
  only via `_resolve_context`'s conditional edge (triggered by a cross-source-join
  `unsupported_reason`), a path on which `planner_error` is never set. The only path that
  ever sets `planner_error` (`route → execute_multi`) never routes to `_refuse`.
- `graph.py:582` (`_execute_multi` aggregation) already has a working `partially_answered`
  disposition — a usable foundation, not something to build from scratch.
- **Four**, not two, independent implementations of cross-source-join detection exist:
  `router.py:139` (`heuristic_multi_plan` — constructs an invalid `MultiQueryPlan(subplans=[])`,
  violates `min_length=1`, raises `pydantic.ValidationError`, pinned by
  `tests/test_router_contract.py::test_defect_heuristic_multi_plan_crashes_on_cross_source_join`),
  `router.py:226` (`heuristic_plan` — returns gracefully), `graph.py:189`
  (`_resolve_context` — the one that currently shadows the others and prevents the crash
  from ever firing, confirmed zero live impact by F9 and by mutation testing), and
  `graph.py:574` (`_synthesize_multi_response` — reachability relative to the `_resolve_context`
  guard is not yet confirmed and must be established during implementation, not assumed).
- `graph.py:805` (PDF native-extraction fallback gate, merged from M2P1): a single
  `if result.confidence < pdf_confidence_threshold` check does not distinguish a structural
  record-miss (confidence 0.0) from a genuinely ambiguous match (confidence 0.4); both fall
  through to vision identically. Confirmed by the M2P1 live-model baseline, Q7.

## 4. Allowed scope

**A. Disposition taxonomy.** Introduce an explicit, named set of terminal dispositions:
`answered`, `partially_answered` (unchanged), `clarification_required`, `unsupported`,
`error`, `refused`. `_unsupported_subresult` (and the single-subplan equivalent) must map
the underlying reason to the correct member of this set instead of hardcoding `refused`.
`_refuse`'s existing error/refused distinction becomes reachable and correct for both its
existing trigger and any newly-routed unsupported-subplan cases.

**B. PDF-path reason distinction (Q7 fix).** `native_lookup`'s confidence gate must carry
forward *why* it fell below threshold (structural non-match vs. low-confidence match), so
`_execute_pdf` can route a structural record-miss straight to `clarification_required`
without invoking vision, while a genuinely ambiguous match continues to fall through to
vision as today.

**C. Cross-source-join guard consolidation.** Map and confirm reachability of all four call
sites identified in §3. Consolidate to a single authoritative check. Fix
`heuristic_multi_plan`'s `MultiQueryPlan(subplans=[])` schema violation so the dead-code
crash cannot resurface if the duplication is ever removed incorrectly later. This is
explicitly **not** removing or loosening the guard's product-level behavior — cross-source
joins are still refused after this milestone. That decision belongs to M2.

**D.** Minimal, local control-flow changes only to the extent required for A–C to be
implemented cleanly and tested.

## 5. Explicitly excluded scope

- Full `graph.py` decomposition/refactor.
- The `ambiguity`-field raw-value leak into user-facing answers (tracked separately in
  `REVIEW_REQUIRED.md`, not part of this milestone).
- The CJK PDF-routing keyword-list gap (tracked separately, not part of this milestone).
- Removing or loosening the cross-source-join product guard itself — an M2 decision.
- Any new M2 capability (cross-source reconciliation logic, door/window fixture).

## 6. Affected surfaces

`apps/api/app/agent/graph.py` (`_unsupported_subresult`, `_refuse`, `_execute_multi`
aggregation, `_execute_pdf`'s confidence gate, `_resolve_context`, `_synthesize_multi_response`),
`apps/api/app/agent/router.py` (`cross_source_join_requested` call sites, `heuristic_multi_plan`),
the document analyzer's `native_lookup` (confidence/reason surface), `apps/api/app/schemas/models.py`
(disposition type, if formalized), `tests/test_router_contract.py`, plus a new
disposition-invariant test module.

## 7. Invariants

- No disposition change may cause a previously-correct `answered` result to become silently
  `answered` with unsupported evidence, or vice versa (no fabrication in either direction).
- All existing tests (142 as of M2P1) continue to pass, or a failing test is independently
  verified as a defect the old behavior was masking — documented, not silently adjusted
  (same standard as the M2P1 F12 rewrite).
- `partially_answered` semantics for existing multi-subplan aggregation are unchanged.
- The cross-source-join product guard continues to refuse cross-source joins after this
  milestone.

## 8. Acceptance criteria

- A disposition-invariant test suite covering every termination path in `graph.py`, each
  asserted against the taxonomy in §4A — not a spot check.
- Q7-shape regression test: a record-miss resolves to `clarification_required` with **zero**
  model calls (assert `fake.calls == []`, same style as M2P1's zero-call tests — no live
  Ollama required).
- `test_defect_heuristic_multi_plan_crashes_on_cross_source_join` rewritten to assert
  graceful behavior instead of pinning the crash.
- Reachability of all four call sites from §3 explicitly proven by test, not assumed.
- Full suite green.

## 9. Documentation / write-back requirements

- New ADR `D-010` documenting the disposition taxonomy decision.
- `docs/decisions/README.md`: update D-004/D-008 status from "documented, unfixed" to
  "resolved, see D-010".
- `REVIEW_REQUIRED.md`: mark the `heuristic_multi_plan` item resolved-by-this-milestone; the
  ambiguity-leak and CJK-routing items remain open, unchanged.
- `PROJECT_STATE.md` milestone history updated on closeout.

## 10. Git, stop conditions

Branch: `feat/m1.5-disposition-contract`. Spec committed to `docs/specs/` as the first
commit, before any code changes. No squash; merge commit only; owner-authorized merge only.
**Stop and report, do not proceed, if:** any existing test needs its asserted *behavior*
changed (not just relocated) without a documented defect rationale; the taxonomy design
requires touching surfaces beyond §6; mapping the four call sites in §3 reveals a fifth.

## 11. Owner decisions — all resolved

- **OD-13 — disposition taxonomy naming. RESOLVED.** Owner confirmed the six-value taxonomy
  (`answered`, `partially_answered`, `clarification_required`, `unsupported`, `error`,
  `refused`) as specified in §4A, with explicit emphasis that this distinction has direct
  business meaning — precise, differentiated outcomes are a hard requirement for M2's
  cross-source reconciliation ("4 of 4 matched" must be distinguishable from "3 of 4 matched,
  1 mismatch" must be distinguishable from "reconciliation could not run"), not cosmetic.
- **OD-14 — Q7-shape fix scope. RESOLVED.** Owner confirmed: once `native_lookup`
  independently determines no matching record exists for the requested board/field, the
  vision fallback must not be invoked. A genuinely ambiguous match (multiple valid
  candidates) continues to fall through to vision as today.
