# SPEC-M2: Cross-Source Reconciliation — Door/Window Schedule Pilot — v1

## 1. Objective

Introduce IFC↔drawing cross-source reconciliation as a narrowly-scoped, first-class
capability — comparing door and window quantities between the IFC model and the
engineering-drawing schedule — while every other cross-source join continues to be
refused exactly as before.

## 2. Rationale

M1.5 built an explicit disposition taxonomy specifically so a compound, item-level
result could be expressed instead of collapsing to a single answered/refused. M2P1
proved deterministic, zero-model-call table extraction works on this fixture's PDF
format — the same technique is reused here, not reinvented. M1.5's own diagnostic
found that the product guard (`cross_source_join_requested`) currently refuses
*every* cross-source question unconditionally. Per explicit owner instruction, this
milestone opens a narrow, specific door — IFC↔drawing door/window reconciliation
only — while every other cross-source shape (electrical panel↔room area, etc.)
remains refused. Finer categorization of what other reconciliation types might
exist in the future is deliberately deferred, not decided now.

## 3. Verified current-state assumptions

Checked directly against `demo_data/armie_demo.ifc` and
`demo_data/armie_demo_schedule.pdf` on `main` @ `f24c99a`, not assumed:

- 4 `IfcDoor` instances: `Name` = `"Level 01 Door 1"`, `"Level 01 Door 2"`,
  `"Level 02 Door 1"`, `"Level 02 Door 2"`. All four share identical
  `Qto_DoorBaseQuantities`: Height=2.1m, Width=0.9m. `Tag` is `None` on all four.
- 4 `IfcWindow` instances, same `Name` pattern. `Qto_WindowBaseQuantities`: Level
  01/02 Window 1 = 1.5×1.2m; Level 01/02 Window 2 = 1.75×1.2m. `Tag` is `None` on
  all four.
- `Tag` (IFC's schema-intended human-facing mark/identifier field, `IfcIdentifier`
  type — a string, same as every other IFC identifier field, including the
  internal `GlobalId`) is the semantically correct join key for this purpose, and
  it is currently unset. `Name` is a tool-generated display label, not a stable
  identity field, and must NOT be used as the join key. `GlobalId` is an internal
  22-character identifier that never appears on a real drawing and is the wrong
  semantic fit even though it is also just a string. Populating `Tag` on the 8
  existing instances (no new entities, no geometry change) is this milestone's
  fixture work — not a pre-existing asset to merely reuse.
- `demo_data/armie_demo_schedule.pdf` is a single-page PDF today
  (`apps/api/app/config.py:18`, `pdf_file = "armie_demo_schedule.pdf"`). It uses
  unruled, position-clustered text (`find_tables()` returns 0, confirmed in the
  M2P1 diagnostic) — the row/column reconstruction path M2P1 built
  (`get_text("blocks")` + word-level y-coordinate clustering) already handles this
  format; a new page in the same style requires no new extraction technique.
- Per D-010 (M1.5), `AgentService._resolve_context` is the single live authority
  for cross-source-join refusal; the other three call sites of
  `cross_source_join_requested` are documented, tested, unreachable duplicates,
  retained only because they are independently unit-tested.
- `partially_answered` (existing, unchanged semantics) means a subplan itself
  failed to execute — it does not mean "the two sources disagree." These are
  different concepts and must not be conflated (owner-confirmed, §4G).

## 4. Allowed scope

**A. Narrow reconciliation detector.** New `cross_source_reconciliation_requested
(question) -> bool` in `router.py`: requires a door/window entity term AND a
reconciliation-intent verb (`compare`, `verify`, `reconcile`, `check`, `match`,
`cross-check`, `consistent`, or Chinese equivalents `核对`/`比对`/`一致`) AND a
schedule/drawing reference term. Deliberately conservative — must not fire on an
ordinary single-source door/window question.

**B. Gate carve-out, single definition.** `cross_source_join_requested` itself
returns `False` whenever `cross_source_reconciliation_requested` matches on the
same question, leaving its existing behavior unchanged for every other case. This
is a change to the shared detection function only, per D-010's discipline — no
individual call site is modified, so all four (one live, three retained-dead)
inherit the carve-out automatically and cannot drift relative to each other again.

**C. Reconciliation plan construction.** New `reconciliation_plan(question,
context)` heuristic, invoked from `_route` alongside the existing heuristic
constructors: when the narrow detector matches, builds a two-subplan
`MultiQueryPlan` — one `source="ifc"` subplan over `IfcDoor`/`IfcWindow` quantities
and `Tag`, one `source="pdf"` subplan over the new schedule page — tagged (e.g.
`intent="reconciliation"`) so `_execute_multi` routes to a dedicated synthesis path
instead of the generic `_synthesize_multi_response`.

**D. Reconciliation synthesis.** New `_synthesize_reconciliation_response`, joining
the two subplan results on the IFC element's `Tag` string (the schedule's Mark
column). Produces one `ReconciliationItem` per element instance found in either
source, each classified:
- `matched` — both sources agree within tolerance (§4F)
- `dimension_mismatch` — both sources have the item, values differ beyond tolerance
- `missing_in_pdf` — present in the IFC model, absent from the schedule
- `missing_in_ifc` — present in the schedule, absent from the IFC model

**E. Fixture.**
1. `demo_data/armie_demo.ifc`: populate `Tag` on the 8 existing `IfcDoor`/
   `IfcWindow` instances per the table below. No new entities, no geometry change,
   no change to any other attribute — this is a targeted edit to one previously-
   empty field on existing instances.
2. `demo_data/armie_demo_schedule.pdf`: add page 2 — a Mark/Level/Type/Width/
   Height schedule table, unruled position-clustered text matching page 1's
   existing layout convention (not a new file — one source of record, consistent
   with the existing M2P1 fixture).

Ground truth:

| Mark | Level | Type | IFC (W×H, m) | PDF schedule (W×H, m) | Intended status |
|---|---|---|---|---|---|
| D01 | L01 | Door | 0.90 × 2.10 | 0.90 × 2.10 | matched |
| D02 | L01 | Door | 0.90 × 2.10 | 0.90 × 2.10 | matched |
| D03 | L02 | Door | 0.90 × 2.10 | 0.90 × 2.10 | matched |
| D04 | L02 | Door | 0.90 × 2.10 | *(row omitted)* | missing_in_pdf |
| W01 | L01 | Window | 1.20 × 1.50 | 1.20 × 1.50 | matched |
| W02 | L01 | Window | 1.20 × 1.75 | 1.20 × 1.70 | dimension_mismatch |
| W03 | L02 | Window | 1.20 × 1.50 | 1.20 × 1.50 | matched |
| W04 | L02 | Window | 1.20 × 1.75 | 1.20 × 1.75 | matched |
| W05 | L02 | Window | *(not in IFC)* | 1.20 × 1.50 | missing_in_ifc |

Assign `D01`–`D04` to the four `IfcDoor` instances in the order listed in §3, and
`W01`–`W04` to the four `IfcWindow` instances in the same order. `W05` exists only
in the PDF schedule (no corresponding IFC edit).

6 matched, 1 dimension_mismatch, 1 missing_in_pdf, 1 missing_in_ifc — all three
originally-planned discrepancy classes covered, count-mismatch expressed as the
missing_in_pdf/missing_in_ifc pair rather than a separate aggregate check.

**F. Tolerance.** ±0.01m per dimension, independently for width and height. The
one designed mismatch (W02: 1.75 vs 1.70) is 0.05m — five times the tolerance,
unambiguous by design; no ground-truth case sits near the boundary.

**G. Disposition mapping.** A reconciliation that executes to completion (both
subplans return data, regardless of any item's match status) is `answered`; the
item-level breakdown lives in a new `reconciliation_items` response field, not
encoded into the disposition itself. `partially_answered`/`error` remain reserved
for one subplan genuinely failing to execute (e.g. the schedule page cannot be
parsed at all) — a system-execution failure, distinct from a content disagreement
between sources.

## 5. Explicitly excluded scope

- Any cross-source shape other than door/window reconciliation (electrical
  panel↔room area, etc.) — remains refused, unchanged, regression-tested.
- Generalizing into a configurable, entity-agnostic reconciliation framework.
  Door/window only, this milestone.
- Frontend/UI rendering of the item-level breakdown — API/schema and orchestration
  only.
- Changing `partially_answered`'s existing semantics for non-reconciliation cases.

## 6. Affected surfaces

`apps/api/app/agent/router.py` (`cross_source_reconciliation_requested`,
`reconciliation_plan`, `cross_source_join_requested`'s single definition),
`apps/api/app/agent/graph.py` (`_route`, `_execute_multi`, new
`_synthesize_reconciliation_response`), `apps/api/app/schemas/models.py`
(`ReconciliationItem`, `ReconciliationStatus`, response field),
`demo_data/armie_demo.ifc` (Tag population only), `demo_data/armie_demo_schedule.pdf`
(new page 2), plus a new reconciliation test module and a regression test for
non-reconciliation cross-source refusal.

## 7. Invariants

- No cross-source shape other than door/window reconciliation may ever bypass the
  refusal — proven by test, not assumed, same discipline as M1.5 §4C.
- A reconciliation `answered` response must never claim `matched` where the
  underlying values differ beyond tolerance, and never claim a mismatch where
  they're within it — no fabrication in either direction.
- The `Tag` edit to `demo_data/armie_demo.ifc` must not add, remove, or otherwise
  modify any entity, geometry, or other attribute — verified by diffing entity
  counts and all non-`Tag` attributes before/after, not merely asserted.
- No new PDF-parsing technique introduced without independent verification;
  M2P1's existing extraction path must be reused as-is for the new page.
- All existing tests (151 as of M1.5) remain green.

## 8. Acceptance criteria

- All 9 modeled items (§4E) reconcile to their intended status exactly.
- At least 2 existing non-reconciliation cross-source-refusal tests
  (electrical-load-vs-room-area and one other) pass unmodified — proving the
  carve-out did not broaden the gate.
- `cross_source_reconciliation_requested` has both positive examples (the pilot
  question shape) and negative examples (an ordinary door/window question with no
  reconciliation verb; a non-reconciliation cross-source question) asserted
  directly.
- Both subplans execute at zero model calls (native IFC query + native PDF text
  extraction), consistent with M2P1/M1.5's cost discipline.
- A direct test confirming the IFC `Tag` edit changed only the 8 targeted
  attributes (entity count, geometry, and all other attributes byte-identical
  before/after).

## 9. Documentation / write-back requirements

- New ADR `D-011` documenting the gate carve-out and the disposition-mapping
  decision (OD-15/16/17/18).
- `PROJECT_STATE.md` milestone entry for M2.
- Fold in the outstanding one-line fix on the M1.5 entry (`"pending review"` →
  `"merged"`) as part of the first non-spec commit.

## 10. Git, stop conditions

Branch `feat/m2-cross-source-reconciliation-pilot`. Spec committed to
`docs/specs/` as the first commit, before any code or fixture changes. No squash;
merge commit only; owner-authorized merge only.

**Stop and report, do not proceed or improvise, if:** the `Tag` edit cannot be
made without touching anything beyond the 8 targeted attributes; the
`cross_source_join_requested` single-definition carve-out cannot be implemented
without touching a surface outside §6; any existing cross-source-refusal test
needs its *assertion* changed (not just relocated) after the carve-out lands.

## 11. Owner decisions

- **OD-15 — gate scope. RESOLVED (owner-explicit).** Limited to IFC↔drawing
  door/window reconciliation only; every other cross-source shape remains
  refused; finer reconciliation sub-categories deliberately deferred, not decided
  now.
- **OD-16 — result representation. RESOLVED (owner-explicit; mechanism per
  Project Control recommendation).** Item-level breakdown required — matched vs.
  mismatched vs. missing, per item, not collapsed. `answered` + structured
  `reconciliation_items`, not `partially_answered`, for a fully-executed
  reconciliation.
- **OD-17 — detector precision, tolerance, fixture ground truth. RESOLVED
  (owner-delegated to Project Control recommendation).** §4A/E/F above.
- **OD-18 — join key field. RESOLVED (owner-explicit).** Match on IFC's `Tag`
  field (populated as part of this milestone), not `Name`. `Name` is a
  tool-generated display label, not a stable identifier; `GlobalId` is an
  internal identifier that never appears on a real drawing. `Tag` is the
  schema-intended, semantically correct field for this purpose.
