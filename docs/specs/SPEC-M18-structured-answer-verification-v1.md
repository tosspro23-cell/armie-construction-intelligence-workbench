# SPEC-M18: Structured Answer-Fact Verification — v1

## 1. Objective

Replace `_narrative_consistent_with_tool_facts` (`apps/api/app/agent/graph.py`) -- the free-text,
character-proximity heuristic that decides whether a V2 answer's own stated numbers get shown as
`verified` -- with a structural mechanism: the model states its final numeric claims through a
dedicated tool call, each one explicitly tied to the entity and measure it describes, and
verification checks those structured claims against this turn's own real tool results directly.
No regex scanning of free text, no character-window proximity guessing, no per-shape exemption
list. This closes, at the root, the entire class of false-positive bugs the proximity heuristic has
produced -- eight confirmed real instances so far, not a hypothetical risk.

## 2. Rationale

`_narrative_consistent_with_tool_facts` has been patched eight times across two live
stress-testing sessions, each time for a different real shape a correct answer's own prose can
take that the character-proximity heuristic could not tell apart from a fabrication:

- **D-065**: a restated element tag/mark with no preceding reference word ("tag", "mark", ...).
- **D-066**: the same gap for plural restatement ("tags X and Y") and a window-slicing bug that
  could bisect a real number sitting at the edge of the scan window.
- **D-068 (two distinct gaps)**: a list's own length recorded as a real fact only inside one
  specific tool's own result shape, never generalized; and `known_reference_numbers` checking only
  two of the three citation-locator keys a real citation can carry.
- **D-069**: a `distinct_value_summary`'s own per-value counts computed *after* the raw facts were
  already extracted, so a real count that only exists inside that summary was never recognized.
- **D-073**: a real IFC GlobalId's own embedded digits, misread as a claimed number because the
  regex has no concept of "this digit sequence is part of an opaque identifier token, not a
  number."
- **D-074 (this spec's own trigger, 2026-09-25, found live minutes after redeploying D-073)**: `"How
  many doors and windows are in this building?"` -> `"There are 14 doors and 24 windows in the
  building."`, both numbers real and correct (`count_elements(IfcDoor)` -> 14,
  `count_elements(IfcWindow)` -> 24) -- flagged `unverified` because `"24"` (a real window count)
  sits within the 15-character scan window around the word `"doors"`, and 24 is not in the door
  call's own expected set. This is not an edge case: reporting two different entities' counts in
  one sentence is the single most natural way to answer a two-part question, and it broke on the
  first real question that happened to ask for two counts together.

Each fix has been correct and proportionate to the shape it closed -- this is not a claim that any
individual patch was wrong. The pattern across all eight is the actual finding:
**character-proximity in free text is not a reliable signal for "which entity does this number
describe," because natural language routinely puts two different entities' own numbers next to
each other, and routinely puts non-numeric identifiers (tags, GlobalIds) that merely *contain*
digit sequences next to entity nouns too.** `docs/decisions/README.md`'s own D-069 entry named
this exact risk and the exact alternative -- *"require every claimed number to trace to a
specific, tagged source value at generation time"* -- and flagged revisiting it "if this exact
check produces a seventh real gap." An eighth has now surfaced, on the single most ordinary
phrasing tested yet. Raised to the owner (2026-09-25); owner's decision: redesign now, per that
original alternative, rather than fix a ninth shape.

**Why this is the same pattern SPEC-M17 already proved works.** `submit_finding_verdict`
(SPEC-M17, D-064 item 2) replaced free-text extraction of a finding's own proposed
width/height with a structured tool call the model must use to state its conclusion -- eliminating
an entire, analogous fabrication-risk class (an unrelated tool result's number being adopted as the
proposed dimension) at the root, not by pattern-matching phrasings. This spec applies the identical
discipline to V2's general narrative-consistency check, which today is the one place in the V2
answer pipeline still relying on inferring structure from prose after the fact.

**A real side benefit, not the motivating reason:** today's check only applies its stricter,
entity-bound half (`_entity_terms_for`) to English answers with a matching alias in
`ELEMENT_ALIASES`, and never to PDF-derived facts at all (`extract_pdf_field`'s plan carries no
`entity_type`, so `_entity_terms_for(None)` returns an empty set -- PDF facts only ever get the
weaker baseline "some real number appears somewhere" check). A structured claim tied to an
explicit `entity`/`measure` pair the model itself names needs no alias table and no source-type
distinction -- a PDF field claim gets exactly the same rigor as an IFC one, and a Chinese answer's
own claim gets exactly the same rigor as an English one. Closing this asymmetry is a consequence of
the redesign, not a separate piece of scope being added.

**Why not keep the heuristic as a fallback.** A version of this design that falls back to the old
character-proximity scan whenever the model doesn't submit structured claims would still leave the
bug class alive, just rarer -- and would mean maintaining two verification mechanisms indefinitely.
This spec retires `_narrative_consistent_with_tool_facts` outright; a turn with a numeric claim and
no structured submission is disclosed as unverified for that specific, honest reason (the model
never confirmed its own numbers structurally), not silently re-checked by the very mechanism being
replaced.

## 3. Verified current-state assumptions

Checked directly against the current codebase, not assumed:

- `AgentService.invoke_v2` (`apps/api/app/agent/graph.py:2475`) accepts `include_verdict_tool:
  bool = False`; when set, `tool_definitions` (`:2536`) is `[*TOOL_DEFINITIONS,
  SUBMIT_FINDING_VERDICT_TOOL]` instead of `TOOL_DEFINITIONS` alone -- the exact, already-proven
  mechanism for offering one additional structured-submission tool on top of the normal toolbox,
  reused here rather than invented fresh.
- `SUBMIT_FINDING_VERDICT_TOOL` (`apps/api/app/agent/tools.py:150-182`) is a `dict` appended to the
  tools list sent to the provider; `_v2_dispatch_tool` (`apps/api/app/agent/graph.py:2283`) special-
  cases `tool_call.tool_name == "submit_finding_verdict"` as a local no-op that returns a fixed
  `{"tool_result": {..., "disposition": "answered", "citations": [], ...}, "finding_verdict":
  tool_call.arguments}` without touching any real IFC/PDF source -- the same shape this spec's new
  tool needs.
- `expected_numeric_facts: list[tuple[frozenset[str], set[float]]]` (`:2580`, built incrementally at
  `:3004-3021` and `:3084-3086`) is the current per-tool-call fact store, keyed by
  `_entity_terms_for(plan.entity_type)` (`:3356-3379`, English-alias-only, empty for any PDF-sourced
  plan since `plan.entity_type` is `None` for `extract_pdf_field`) and populated by
  `_numeric_tokens_from_result_value` (`:3163`).
- `known_reference_numbers` (`:2836-2845`) is built once per turn from `all_citations`' own
  `tag`/`record`/`mark` locator keys, independent of `expected_numeric_facts`.
- `_narrative_consistent_with_tool_facts` (`:3382`) is called once (`:2846`), and its `bool` result
  gates `verification.status` between `"unverified"`/`"verified"`/`"not_applicable"` (`:2867-2871`)
  -- disposition, citations, and the narrative text itself are never altered by this check (D-062's
  owner decision: disclose a caveat, never withhold or replace a real answer).
- This exact call site is the *only* place `_narrative_consistent_with_tool_facts` is invoked in
  the codebase (confirmed: `grep -n _narrative_consistent_with_tool_facts apps/api/app/agent/
  graph.py` returns only its own definition and this one call, plus comment references) -- no
  other caller needs updating.
- `submit_finding_verdict`'s own `confirmed_width_m`/`confirmed_height_m` validation
  (`AgentService.build_finding_proposal`, `_matches_known_value`) is a separate mechanism, entirely
  independent of `_narrative_consistent_with_tool_facts` -- it validates a finding's *specific*
  proposed dimension against that finding's own known IFC/PDF values, not general answer prose.
  Confirmed unaffected by anything in this spec's scope.
- V1's own "answer polish" number-preservation guard (`_polish_preserves_facts`, SPEC-M10) is a
  different, already-structural check (exact-set equality between the original and reworded text's
  own numbers) -- unrelated to and unaffected by this spec.

## 4. Allowed scope

**A. New tool: `submit_answer_facts`** (`apps/api/app/agent/tools.py`, alongside
`SUBMIT_FINDING_VERDICT_TOOL`, same "offered conditionally, not in the base `TOOL_DEFINITIONS`
list" pattern):

```python
SUBMIT_ANSWER_FACTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_answer_facts",
        "description": (
            "Call this exactly once, immediately before your final answer, whenever that answer "
            "states any number. List every number your answer states, each tied to the specific "
            "entity and measure it describes -- e.g. if your answer says '14 doors and 24 "
            "windows', submit two claims: {entity: 'IfcDoor', measure: 'count', value: 14} and "
            "{entity: 'IfcWindow', measure: 'count', value: 24}. Every claim is checked against "
            "this turn's own real tool results before your answer is shown as verified. If your "
            "answer states no numbers at all, call this with an empty claims list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string", "description": "What this number is about -- the IFC entity type (e.g. 'IfcDoor'), a specific tag/mark, or a PDF record identifier (e.g. 'Panel-B'), exactly as your tool call(s) this turn used it."},
                            "measure": {"type": "string", "description": "What aspect of the entity this number measures -- e.g. 'count', 'width_m', 'height_m', 'connected_load', 'max_height_m'."},
                            "value": {"type": "number"},
                        },
                        "required": ["entity", "measure", "value"],
                    },
                },
            },
            "required": ["claims"],
        },
    },
}
```

`entity`/`measure` are free-form strings, matching this project's existing free-form
`entity_type`/`requested_field` convention (`apps/api/app/schemas/models.py`) -- not a new enum or
ontology (§6).

**B. Offer it on every V2 turn.** `invoke_v2`'s `tool_definitions` construction (`:2536`) becomes
`[*TOOL_DEFINITIONS, SUBMIT_ANSWER_FACTS_TOOL, *([SUBMIT_FINDING_VERDICT_TOOL] if
include_verdict_tool else [])]` -- offered unconditionally, unlike `submit_finding_verdict` which
stays investigation-only. A finding-investigation turn gets both tools: `submit_finding_verdict`
for the finding's own typed verdict (unchanged, §3), `submit_answer_facts` for the turn's general
narrative-consistency check (this spec) -- the two are independent and both apply.

**C. Dispatch as a local no-op**, mirroring `submit_finding_verdict`'s own handling in
`_v2_dispatch_tool` exactly: record `tool_call.arguments["claims"]` onto `state` (a new
`state["answer_facts"]` key, analogous to `state["finding_verdict"]`) and return the same
"answered, no real citations" shape.

**D. Replace `expected_numeric_facts`'s consumer.** Keep collecting real per-call facts largely as
today, but keyed by `(entity, measure) -> set[float]` instead of `(entity_terms, set[float])` --
`_numeric_tokens_from_result_value` stays as the source of *what numbers a raw tool result
actually contains*; a new mapping step assigns each to a `measure` key
(`"count"` for a scalar count result, `"width_m"`/`"height_m"`/etc. for a property/quantity
lookup, the PDF `field` name for `extract_pdf_field`) instead of the current flat, unlabeled
`set[float]`. `entity` becomes the plan's own `entity_type` (IFC) or the PDF lookup's own record
identifier extracted the same way citations already are, not an alias-table lookup.

**E. New verification function**, replacing `_narrative_consistent_with_tool_facts`:

```python
def _answer_facts_verified(claims: list[dict] | None, real_facts: dict[tuple[str, str], set[float]]) -> tuple[bool, str]:
```

Returns `(all_matched, reason)`. For each claim, look up `(claim["entity"], claim["measure"])` in
`real_facts`; a match requires the same tolerance rule already established
(`_matches`: exact for a whole-number expectation, `< 0.05` absolute for a fractional one) against
*any* value in that key's own set (a key can carry more than one real value, e.g. two different
tool calls both touching the same entity/measure this turn). No entity-alias table, no character
window, no regex over `answer_markdown` at all.

**F. Three-way verification outcome** at the existing call site (`:2867-2871`), replacing the
current two-way one:

1. No citations this turn -> `not_applicable` (unchanged).
2. Citations exist, the answer text contains no digit characters at all -> `verified` (nothing
   numeric was claimed; nothing to check).
3. Citations exist, the answer contains digits, `state.get("answer_facts")` is `None` (the model
   never called the tool) -> `unverified`, reason: *"This answer states a number, but its own
   facts were never submitted for structural confirmation."* -- an honest, more specific signal
   than today's generic message, not a silent pass.
4. Citations exist, claims were submitted, every claim matches a real fact -> `verified`.
5. Citations exist, claims were submitted, at least one claim doesn't match -> `unverified`,
   reason names the specific unmatched claim (`entity`, `measure`, claimed `value`) -- strictly
   more actionable than today's generic "could not be matched" message.

**G. `known_reference_numbers`, `_entity_terms_for`, `_numeric_tokens_from_result_value`'s own
list-length/`distinct_value_summary` special-casing, and `_narrative_consistent_with_tool_facts`
itself are deleted** once the new mechanism is in place -- not kept as dead code, and not kept as a
fallback (§2's own "why not keep the heuristic as a fallback").

## 5. Explicitly excluded scope

- No enum or formal ontology for `entity`/`measure` -- free-form strings, matching this project's
  existing convention. A future milestone may add one if the open-string approach proves too loose
  in practice; not assumed here.
- `submit_finding_verdict`'s own `confirmed_width_m`/`confirmed_height_m` validation
  (`build_finding_proposal`, `_matches_known_value`) is untouched -- a different, already-correct,
  already-structural mechanism for a finding's own specific proposed value.
- V1's answer-polish number-preservation guard (`_polish_preserves_facts`, SPEC-M10) is untouched
  -- a different, already-structural check.
- No change to `disposition`, citations, or the narrative text itself -- only
  `verification.status`/`reason` construction changes, matching D-062's existing "disclose, never
  withhold" decision, which this spec does not revisit.
- No attempt to make the model *more likely* to call `submit_answer_facts` beyond a clear tool
  description (§4A) -- if this proves unreliable in practice (outcome 3 above firing often on
  otherwise-correct answers), that is a real, measurable fast-follow question, not solved
  speculatively here.
- No retrofit of V1's own natural-language answer construction (`_natural_answer`) -- V1 builds its
  prose deterministically from already-computed values and has no analogous free-text-scanning
  verification step to replace.

## 6. Affected surfaces

| File | Change |
|---|---|
| `apps/api/app/agent/tools.py` | New `SUBMIT_ANSWER_FACTS_TOOL` definition. |
| `apps/api/app/agent/graph.py` | `invoke_v2` (offer the new tool unconditionally); `_v2_dispatch_tool` (local no-op handling, mirroring `submit_finding_verdict`); replace `expected_numeric_facts`'s `(entity_terms, set[float])` shape with `(entity, measure) -> set[float]`; new `_answer_facts_verified`; delete `_narrative_consistent_with_tool_facts`, `_entity_terms_for`, and the now-unused parts of `_numeric_tokens_from_result_value`'s own special-casing (D-068/D-069's own list-length/`distinct_value_summary` logic becomes the new mapping step's job, not a flat-set accumulator's). |
| `tests/test_v2_representative_eval.py` | Existing narrative-consistency tests (D-065, D-066, both D-068 gaps, D-069, D-073, and this spec's own D-074 repro) rewritten against the new mechanism -- see §9's own git/stop-condition note. |
| `tests/test_engineering_findings.py` | Investigation-turn tests that assert on `verification.status` reviewed for continued correctness under the new mechanism (structured `submit_finding_verdict` calls in these tests do not themselves state answer-prose numbers requiring `submit_answer_facts`, but the fake-provider scripts need updating if the new tool is now unconditionally offered and the test asserts on the exact tool list). |
| `docs/decisions/README.md` | New `D-074` entry recording this redesign and the full D-065..D-074 history. |
| `docs/decisions/REVIEW_REQUIRED.md` | The existing "narrative-consistency... six distinct real causes... redesign" entry marked `RESOLVED by SPEC-M18`. |
| `PROJECT_STATE.md` | New M18 milestone entry. |

## 7. Invariants

1. A verification failure still produces a disclosed caveat; the answer, its citations, and its
   disposition are never withheld or altered because of a verification result (D-062, unchanged).
2. `submit_finding_verdict`'s own structured verdict/dimension validation is unaffected -- a
   finding investigation's proposal-approval path does not depend on this spec's own mechanism.
3. No claimed number is ever treated as confirmed without matching a real, this-turn tool result
   -- the redesign changes *how* a claim is matched (structural lookup instead of proximity
   scanning), never *whether* an unmatched claim can pass.
4. English-only and PDF-vs-IFC asymmetries in today's check are closed as a consequence of the
   redesign (§2), not treated as separate scope requiring their own acceptance criteria.

## 8. Acceptance criteria

- The exact D-074 repro (`"How many doors and windows are in this building?"` against the real
  Duplex Apartment building, or an equivalent fixture-backed unit test) verifies correctly under
  the new mechanism.
- Every prior repro this session's own test suite carries for D-065, D-066 (both gaps), D-068
  (both gaps), D-069, and D-073 is rewritten (not deleted) to exercise the *same underlying claim*
  -- "a correct answer restating this real shape is verified; a genuinely fabricated one is still
  caught" -- against `submit_answer_facts`/`_answer_facts_verified` instead of the retired
  heuristic. A prior fix's own regression coverage must not simply disappear because its mechanism
  changed.
- A genuinely fabricated claim (e.g. a door count submitted as `99999` when the real
  `count_elements` result was `14`) is still caught and marked `unverified`.
- A turn whose answer states a number but never calls `submit_answer_facts` is marked `unverified`
  with the new, specific reason (§4F outcome 3) -- confirmed via a scripted fake-provider turn,
  not asserted from code reading alone.
- A turn whose answer states no numbers at all (e.g. "Which storey has the most windows? -> Level
  01") is `verified` without requiring an empty `submit_answer_facts` call to be scripted in every
  such existing test (backward-compatible with today's fixture scripts that don't call it).
- `PYTHONPATH=apps/api python3 -m pytest -q` and `(cd apps/web && npm run build)` both pass with no
  flags.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- Live-verified against the real deployed app on at least one real Dataset Pack building (Duplex
  Apartment or RWTH DigitalHub) with the exact D-074 phrasing, not only against local fixtures --
  matching this project's own established "measure against the real thing, not just tests" practice
  for this specific check's history.

## 9. Documentation requirements

- `docs/decisions/README.md` D-074: the full history from §2 (all eight prior gaps, briefly, with
  pointers to their own existing entries) plus this redesign's own design and verification account.
- `docs/decisions/REVIEW_REQUIRED.md`: mark the standing narrative-consistency entry `RESOLVED by
  SPEC-M18`, with a short account of what replaced it.
- `PROJECT_STATE.md`: new M18 milestone entry, and its own top summary (§ "Current implementation"
  / "Known limitations") updated in the same pass, per the standing-drift lesson recorded in that
  file's own header after this session's earlier correction.

## 10. Git / stop conditions

- **Existing test behavior will change deliberately, with a documented rationale**: every
  `test_v2_representative_eval.py` test asserting on `_narrative_consistent_with_tool_facts`'s own
  mechanics (character windows, `known_reference_numbers`, entity-alias terms) has its *assertion*
  rewritten to target the new mechanism -- this is exactly the "documented defect rationale in the
  commit message" carve-out CLAUDE.md's own stop-condition allows, not a silent behavior change.
  This whole spec is that rationale.
- One commit per lettered subsection of §4 (tool definition; dispatch wiring; fact-collection
  reshape; new verification function + call-site rewrite; test rewrites; docs), matching this
  project's own "reviewable, revertible" commit discipline.
- Branch: `feat/m18-structured-answer-verification`.
- If, during implementation, `_numeric_tokens_from_result_value`'s existing special-casing (list
  length, `distinct_value_summary`) turns out to not cleanly decompose into a `(entity, measure)`
  mapping for some real tool-result shape not enumerated in §4D, stop and report rather than
  inventing a new measure-naming convention ad hoc -- that is exactly the kind of "task's stated
  affected-surfaces list turns out to be incomplete" case CLAUDE.md's own stop conditions name.

## 11. Owner decisions

- **OD-55 (recorded from the 2026-09-25 conversation that authorized this spec)**: given the choice
  between a ninth narrow patch and a redesign at this exact "eighth real gap" threshold
  (`docs/decisions/REVIEW_REQUIRED.md`'s own standing question), the owner chose redesign.
- **OD-56**: retire `_narrative_consistent_with_tool_facts` entirely rather than keep it as a
  fallback for turns without a structured submission (§2's own "why not keep the heuristic as a
  fallback" reasoning) -- confirm this is the intended trade-off (a turn without structured claims
  becomes honestly `unverified` rather than silently re-checked by the mechanism being replaced)
  before implementation proceeds past §4C.
