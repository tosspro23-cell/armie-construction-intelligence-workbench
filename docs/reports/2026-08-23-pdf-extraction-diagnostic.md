# M2 Pre-Milestone Diagnostic — PDF extraction path

Read-only investigation requested by Project Control ahead of M2 scoping. No code was
changed to produce this report; it records findings only.

## Ground truth (`demo_data/armie_demo_schedule.pdf`, one page, clean text layer)

| Board | Connected Load (kW) | Diversity Factor |
|---|---|---|
| DB-L1-A | 18.50 | 0.75 |
| DB-L2-B | 26.00 | 0.65 |
| Panel-A | 44.50 | 0.70 |

Column header is literally `Board Connected Load (kW) Diversity Factor`.

## Part 1 — Confirm or refute (static)

**H1 — CONFIRMED.**
`apps/api/app/tools/document/analyzer.py:87`:
```python
confidence = 0.55 if match else 0.0
```
This is the only confidence value `native_lookup` ever produces (0.55 on a match, 0.0
otherwise) — no other line in `analyzer.py` assigns a native-text confidence.
`apps/api/app/config.py:40`: `pdf_confidence_threshold: float = 0.75`. The gate at
`apps/api/app/agent/graph.py:868` (`if result.confidence < self.settings.pdf_confidence_threshold`)
is therefore true on *every* call, match or no match — confirmed no code path can raise
native confidence above 0.55, let alone above 0.75. The native path can structurally never
serve as a final answer source at the default threshold.

**H2 — CONFIRMED**, with the exact mechanism traced further than the hypothesis states.
`target_board`'s regex (`analyzer.py:116`) requires an `SMDB-` or `DB-` prefix with at
least two hyphenated segments, so it matches `DB-L1-A` and `DB-L2-B` but never `Panel-A`.
The consequence is not exactly "falls into `ambiguity_policy`'s branch" in general — tracing
`_execute_pdf` (`graph.py:786-820`) shows it's specifically the **"Total Connected Load" /
no-board branch** that fires: for "connected load" questions, `target_field`
(`analyzer.py:124-125`) returns `"Total Connected Load"`, and `ambiguity_policy`
(`analyzer.py:134-138`) fires whenever `field == "Total Connected Load" and not board`.
Since `target_board("Panel-A question")` is `None`, "connected load for Panel-A" hits this
branch and returns *"The drawing contains multiple Total Connected Load values. Please
specify the distribution board... for example DB-L1-A."* — even though the user named
Panel-A explicitly. For "diversity factor for Panel-A", `target_field` returns
`"Diversity Factor"`, not `"Total Connected Load"`, so `ambiguity_policy` does *not* fire
(its check is field-specific, not board-specific) — that question instead falls through to
`native_lookup` (with `board and field` false since `board` is `None`), which per H1 always
returns confidence 0.0 and routes to vision. So H2 is confirmed for the connected-load
question specifically; the diversity-factor question is a different failure mode
(native/vision fallback, not the ambiguity-policy message) — worth not conflating in the M2
discussion.

**H3 — PARTIALLY CONFIRMED; the stated mechanism is refuted, the conclusion holds by a
different path.**
- `target_field` does return the literal strings `"Total Connected Load"`
  (`analyzer.py:125`) and `"After Diversity Load"` (`analyzer.py:127`), and direct PDF text
  extraction confirms neither substring — nor any close variant — appears anywhere in the
  fixture's text layer (dumped below). So the second sub-claim, "does `'After Diversity
  Load'` correspond to any column in the fixture," is **refuted as a no**: the fixture has
  exactly three columns (`Board`, `Connected Load (kW)`, `Diversity Factor`); there is no
  after-diversity/derated column at all. That field is not merely mislabeled, it doesn't
  exist in this fixture.
- However, `native_lookup` (`analyzer.py:76-112`) never calls `target_field()` and never
  uses its output as the search needle. The needle is `query.field.lower()`
  (`analyzer.py:85`), and `query.field` is constructed at `graph.py:789` as
  `plan.requested_field or state["question"]`. Tracing `requested_field`'s origin in the
  heuristic router (`router.py:219`, `router.py:233`), it is set to the **raw user question
  string**, not to `target_field()`'s canonical label. So for a heuristically-routed PDF
  question, the actual substring search is "does any PDF text line contain the full
  lowercased question sentence" — which will essentially never match regardless of what the
  column header says. `target_field()`'s canonical strings *are* used, but only in
  `ambiguity_policy` (H2's mechanism) and in the vision-path prompts
  (`board_candidates`/`verify_board_candidate`, `analyzer.py:169,193`) — not in
  `native_lookup`.
- Net effect: the hypothesis's outcome ("native substring match fails, confidence stays
  0.0") is correct, but for board-detected questions the search never even gets
  label-shaped — it's comparing a full sentence against short PDF text lines. Whether the
  LLM semantic-planning path (as opposed to the heuristic path) ever populates
  `requested_field` with something closer to `target_field()`'s output could not be
  determined statically — that would depend on the model's free-text output for that schema
  field, which a live run (Part 2, blocked — see below) would have been needed to observe.

Native PDF text layer, exact dump for the record:
```
ARMIE DEMO ENGINEERING SCHEDULE
Synthetic public fixture · not a real project document
Board
Connected Load (kW)
Diversity Factor
DB-L1-A
18.50
0.75
DB-L2-B
26.00
0.65
Panel-A
44.50
0.70
Notes
All identifiers and values in this drawing are synthetic demo data generated for ARMIE AI Labs.
Use the workbench to retrieve fields with page and region evidence.
```
Note the extractor returns each cell as its own line (column-major-ish per PyMuPDF's
`"text"` mode) — even a canonical column-label needle like `"Connected Load (kW)"` would
need to match a *whole line*, and none of the row lines contain the label text at all
(labels and values are on separate lines from separate table cells). So H1's threshold
defect would still block native answers even for a hypothetically well-labeled needle.

## Part 2 & Part 3 — blocked, not run

Checked for a live Ollama instance in this environment:
```
$ which ollama          → not found
$ curl 127.0.0.1:11434/api/tags → connection refused (exit 7)
```
No Ollama binary and no reachable Ollama service exist in this sandbox, and there is no
network path here to install/pull one. Per the task's explicit stop condition, no fake
provider was substituted and no results were simulated. **Part 2 (nine questions × 2 runs)
and Part 3 (threshold probe) were not run.** They require an environment with a live
Ollama daemon and the `qwen3:8b`/`qwen3-vl:8b` models pulled — the local operation
instructions in `PROJECT_STATE.md`/`README.md` describe that setup. Project Control should
re-run this investigation (or delegate it) in a session with Ollama available to complete
Parts 2–3.

## Observations outside H1–H3

1. **The H2/H3 interaction is more tangled than three independent hypotheses.** Which
   failure mode a question hits (`ambiguity_policy` message vs. native-then-vision fallback
   vs. board-localized vision) depends on the *combination* of whether `target_board`
   matches and what `target_field` returns, not on H1/H2/H3 in isolation. The Panel-A
   connected-load and Panel-A diversity-factor questions take genuinely different code
   paths for that reason — worth keeping distinct in any M2 write-up rather than treating
   "Panel-A fails" as one bug.
2. **For `DB-L1-A`/`DB-L2-B` questions, `native_lookup` is never reached at all** (both
   `board` and `field` resolve, so `graph.py:809`'s `if board and field:` sends every one of
   those four questions straight into the board-localized *vision* flow). H1's threshold
   defect is real but is dead code for those four questions — it only matters for the
   `board is None` fallback questions. This narrows H1's practical blast radius relative to
   how the hypothesis reads standalone.
3. **`requested_field`'s provenance for the LLM/semantic-planning path is untraced here**
   (static-only investigation, no live run) — it cannot be ruled out that model-generated
   `requested_field` values behave differently from the heuristic router's "whole question
   as field" behavior. This is exactly the kind of thing Part 2 would have surfaced and
   couldn't.
4. Nothing in `analyzer.py`, `graph.py`, or `router.py` hardcodes or special-cases the three
   ground-truth boards/values — all three go through the same generic code paths, so the
   ground-truth table's specific numbers are not relevant to the static analysis beyond
   confirming what should/shouldn't match textually.

## Project Control addendum

Recorded from review, ahead of any M2 scoping decision:

(a) PyMuPDF's `get_text("text")` returns one line per table cell, so no line contains both
a board name and its value — line-substring matching cannot do table lookup regardless of
the needle chosen. This is consistent with the H3 trace above: even a canonical
column-label needle would still fail, because labels and values never share a line.

(b) `get_text("blocks")` already groups each table row into one block, and a words +
y-coordinate clustering reconstructs rows exactly and yields bbox coordinates for free.

(c) `find_tables()` returns zero tables on this fixture (no ruled lines).

These are recorded as inputs to a future M2 scoping decision, not as an approved design —
no implementation approach is adopted by this report.
