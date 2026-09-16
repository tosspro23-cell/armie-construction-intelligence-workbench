# SPEC-M14 — A deterministic fast path for Chinese count/group-by IFC questions

## Objective

Give a defined, narrow set of Simplified Chinese IFC count and storey-group-by questions the same
zero-model-call deterministic fast path English already has, so asking "这个项目里有多少扇门？" costs
the same as "how many doors are there?" — a real model call today, regardless of how simple the
question is.

## Rationale

Owner-asked, 2026-09-16, after a session's worth of Chinese-language testing: is Chinese recognition
today done through the LLM, and can a fast path like English's be added, covering all functionality?
The second half (full parity across every operation — quantity extrema, space distance, viewer-
snapshot phrases, PDF/electrical-schedule detection) is real but large scope; the owner agreed to
start with count/group-by, the two operations that cover the large majority of real IFC questions
asked this session.

**Confirmed by direct testing, not assumed: today, yes — nearly every Chinese IFC question reaches
the LLM planner.** `ELEMENT_ALIASES` (`apps/api/app/agent/router.py`), the single table every
heuristic entity-recognition call site reads, has zero Chinese keys. A few narrow exceptions already
exist and are unaffected by this spec: `cross_source_reconciliation_requested`/
`cross_source_join_requested`'s intent guards, a handful of exact `DEICTIC_TERMS` phrases ("这个是
什么"), and one hardcoded ambiguity clarification for "这张图里的板有多少" (`graph.py`
`_resolve_context`). None of these recognize a general Chinese count/group-by question.

**A real, load-bearing technical fact, verified empirically, not assumed:** Python's `\b` word-
boundary regex — used at every current `ELEMENT_ALIASES` lookup site — never matches inside
Chinese text, even for a correct term:

```pycon
>>> import re
>>> bool(re.search(r"\b门\b", "这个项目里有多少扇门？"))
False
```

`\b` only fires at a transition between a "word" character and a non-word one; adjacent Chinese
characters are all `\w` under Python 3's Unicode-aware `re`, so there is never a boundary between
two ideographs in unsegmented text (no spaces). Simply adding Chinese keys to `ELEMENT_ALIASES`
without also fixing the matching itself would silently continue to match nothing. The existing
`cross_source_reconciliation_requested`/`cross_source_join_requested` functions already avoid this
correctly — their Chinese terms (`门|窗`, `核对|比对`) sit outside the `\b...\b` group, as bare
substring alternatives — this spec generalizes that same, already-proven technique rather than
inventing a new one.

**A second real fact, verified by tracing the graph, not assumed: the actual routing decision does
not depend on `fast_path_coverage`'s own `ascii_only` gate the way its docstring implies.**
`AgentService._route` (`graph.py`) computes `generic_multi = heuristic_multi_plan(...)`
unconditionally and uses it immediately, before `fast_path_coverage`'s own `coverage_status` or the
derived `use_fast_path` boolean are even consulted:

```python
if generic_multi is not None:
    multi_plan = generic_multi
elif nearest_space_requested(...):
    ...
elif use_fast_path:
    ...
else:
    # LLM planner
```

`heuristic_multi_plan` itself has no `ascii_only` gate at all — it already runs its own
(English-only, substring-based) count/grouping keyword checks directly. This means the primary,
sufficient fix for the owner's actual request is inside `heuristic_multi_plan` (and, for
consistency, `heuristic_plan`, its single-entity-only sibling used when `heuristic_multi_plan`
returns `None`) — `fast_path_coverage`'s own `ascii_only` gate is a secondary, audit-honesty fix
(see Invariants), not the primary blocker.

**Two real term-collision risks found while drafting the alias list, not discovered later:**

- **"板" (a bare "board/slab") is already deliberately treated as ambiguous elsewhere in this
  codebase** — `AgentService._resolve_context` special-cases "这张图里的板有多少"/"图里的板有多少"
  specifically because it could mean an IFC `IfcSlab` or a PDF electrical panel/board. Aliasing bare
  "板" to `IfcSlab` here would silently resolve that same ambiguity the wrong way for every other
  phrasing of it. Only the unambiguous compound "楼板" (floor slab) is aliased; bare "板" is not.
- **"面板" (panel/plate) has the identical ambiguity** (IFC `IfcPlate`'s curtain-wall glazing infill
  vs. an electrical panel from a PDF schedule) and **"构件" is this codebase's own generic Chinese
  fallback noun for "unknown element type"** (`graph.py`'s response-formatting noun maps, D-055),
  not a specific alias for `IfcMember` — aliasing it here would make a generic "构件有多少" collide
  with the narrow `IfcMember` type. Both are excluded from this spec's alias list (see Explicitly
  excluded scope); English has no equivalent generic-word collision, since "component"/"element"
  were never aliased there either.

## Verified current-state assumptions

- `response_language` selection (`"zh-CN" if any("一" <= char <= "鿿" for char in
  question) else "en"`) already exists in both `heuristic_multi_plan` and `reconciliation_plan`,
  and the Chinese response-formatting noun/measure-word maps already cover every `SUPPORTED_ENTITY_
  TYPES` value (D-055) — this spec only has to make a correct **plan** reach that already-Chinese-
  aware rendering path; no response-formatting work is needed.
- `heuristic_multi_plan`'s own entity-type loop imposes no connector requirement ("and"/"和") —
  it finds every alias mentioned anywhere in the text independently, so "门和窗各有多少" already
  works exactly like "how many doors and windows are there" once both aliases exist and resolve.
- `capability_gate` (`SUPPORTED_ENTITY_TYPES`, derived from `ELEMENT_ALIASES.values()`) needs no
  change — a Chinese alias resolving to an existing canonical `Ifc*` string automatically clears
  the same gate an English alias for the same type already clears.
- `average`/`sum`/`min`/`max`/`get_properties` are **not** part of English's own fast-path
  completeness today either (`heuristic_plan`'s `complete` formula only accepts `operation ==
  "count"`, `"get_properties"`, or a fully-explicit grouping) — Chinese parity for count/group-by
  therefore reaches full parity with what English's fast path itself actually covers, not a subset
  of it.

## Allowed scope

### A. A language-aware alias-matching helper, used everywhere `ELEMENT_ALIASES` is matched (own commit)

A small helper in `router.py`, e.g. `_alias_search(term, text) -> re.Match | None`: returns
`re.search(rf"\b{re.escape(term)}\b", text)` for an ASCII term (unchanged English behavior, exact
byte-for-byte), and `re.search(re.escape(term), text)` (bare substring, no `\b`) for a non-ASCII
term. Replaces the three existing inline `re.search(rf"\b{re.escape(key)}\b", ...)` call sites
(`fast_path_coverage`'s `entities` comprehension, `heuristic_multi_plan`'s `entity_types` loop,
`heuristic_plan`'s `entity_type = next(...)` line) — English behavior is provably unchanged (same
pattern string for every existing ASCII key), Chinese keys become matchable for the first time.

### B. Chinese entity aliases added to `ELEMENT_ALIASES` (own commit)

| Chinese term(s) | Canonical type | Note |
|---|---|---|
| 门 | `IfcDoor` | |
| 窗, 窗户 | `IfcWindow` | |
| 墙, 墙体 | `IfcWall` | |
| 空间, 房间 | `IfcSpace` | mirrors English's own space/room pair |
| 楼梯 | `IfcStair` | |
| 楼板 | `IfcSlab` | bare "板" deliberately excluded — see Rationale |
| 屋顶 | `IfcRoof` | |
| 柱, 柱子 | `IfcColumn` | |
| 梁 | `IfcBeam` | |
| 栏杆 | `IfcRailing` | |
| 饰面 | `IfcCovering` | |
| 家具 | `IfcFurnishingElement` | |
| 基础 | `IfcFooting` | |

`IfcMember`/`IfcPlate`/`IfcBuildingElementProxy` get no Chinese alias in this phase — see
Explicitly excluded scope.

### C. Chinese count/grouping/argmax/argmin keyword sets (own commit)

Added as additional substring alternatives (not `\b`-wrapped, matching this file's existing
Chinese-marker style in `cross_source_reconciliation_requested`) everywhere the equivalent English
tuple already drives `heuristic_multi_plan`'s `count_intent`/`grouping`/`argmax`/`argmin` and
`heuristic_plan`'s `recognises_ifc` operation/`group_by`/`postprocess` derivation:

- Count intent: 有多少, 数量, 共有, 一共有, 统计
- Storey grouping: 按楼层, 按层, 每层, 每一层, 各楼层, 分楼层, 楼层分布
- Argmax (most): 最多
- Argmin (fewest): 最少

### D. `fast_path_coverage` audit-honesty fix (own commit)

`fast_path_coverage`'s `ascii_only` gate is relaxed to `ascii_only or contains_chinese` (the same
`一`-`鿿` range check already used elsewhere), and its own `generic_count`/
`generic_grouping` checks gain the same Chinese keyword alternatives as §C, so its `coverage_status`
now correctly reports `"complete"` (and the audit trail's `planning_mode` correctly says
`"heuristic"`, not `"llm"`) for exactly the same Chinese questions §A-C already make
`heuristic_multi_plan` answer deterministically. This does not change `_route`'s actual behavior
(§A-C already made it deterministic beforehand) — it corrects what the audit trail says about it.
Any other non-ASCII, non-Chinese script continues to report `"incomplete"`, since none of its
literal term lists match anything outside English/Chinese.

### E. Tests (own commit)

- `tests/test_router_contract.py`: Chinese-language equivalents of the existing English
  `heuristic_plan`/`heuristic_multi_plan`/`fast_path_coverage` parametrized cases — single-entity
  count, multi-entity count ("门和窗各有多少"), storey grouping ("按楼层统计窗户数量"), argmax/argmin
  ("哪层的门最多"/"最少") — each asserting the same `entity_type`/`operation`/`match_status`
  shape its English counterpart already asserts.
- `test_fast_path_coverage_incomplete_for_non_ascii_question` (existing) is renamed and its
  asserted behavior deliberately flipped for the one question it uses — see Invariants for why
  this is an intentional, spec-approved change, not a silent regression.
- A live, zero-model-call end-to-end case via `AgentService.invoke()` (mirroring
  `tests/test_dataset_pack_corpus.py`'s own harness pattern) proving a real Chinese count question
  against a real Dataset Pack project returns `answered`/`model_call_count == 0`, not just that the
  planning-layer helpers return the right shape in isolation.

## Explicitly excluded scope

- **No Chinese parity for quantity extrema ("最高的门"), space distance ("从...到...的距离"),
  viewer-snapshot phrases, or PDF/electrical-schedule domain detection.** English itself does not
  fast-path most of these either (Verified current-state assumptions) or they carry their own
  separate ambiguity/scope questions (electrical-schedule detection crossing into the PDF domain)
  better suited to their own pass.
- **No alias for `IfcMember` ("构件") or `IfcPlate`/bare `IfcSlab` ("面板"/"板").** Both are
  genuinely ambiguous with an existing generic-fallback-word or cross-domain (PDF panel/board)
  usage already present in this codebase — see Rationale. Revisiting either needs its own
  disambiguation design, not a default alias.
- **No Traditional Chinese, no mixed Chinese/English question support, no dialect/pinyin input.**
  Simplified Chinese only, matching every existing Chinese string literal already in this codebase.
- **No change to the semantic (LLM) planner's own prompt or its Chinese-language capability** —
  it already receives and can answer Chinese questions today (correctly, just slower/costlier);
  this spec only shortcuts the subset that no longer needs it.

## Affected surfaces

- `apps/api/app/agent/router.py` — `_alias_search` (new), `ELEMENT_ALIASES`, `fast_path_coverage`,
  `heuristic_multi_plan`, `heuristic_plan`.
- `tests/test_router_contract.py` — new Chinese-parity cases; one existing test renamed with its
  assertion intentionally flipped (see Invariants).
- `docs/decisions/README.md` — new `D-057` entry (OD-49 resolution).
- `PROJECT_STATE.md` — `M14` milestone entry.

No change to `apps/api/app/agent/graph.py`, `plan_validation.py`, response-formatting noun maps
(already Chinese-aware, D-055), or any frontend file.

## Invariants

- Zero new model calls for any question this spec makes deterministic — the whole point of a fast
  path.
- English behavior is provably byte-for-byte unchanged: `_alias_search` returns the exact same
  pattern string for every existing ASCII key, and every new Chinese keyword list is additive
  (`or`-ed alongside, never replacing, the existing English tuples).
- **`fast_path_coverage`'s own docstring today asserts "Any non-ASCII ... request is routed to the
  typed semantic planner" as a deliberate invariant.** This spec knowingly and explicitly reverses
  that invariant for the narrow, enumerated Chinese count/group-by surface in §B-D — recorded here
  as OD-49, not silently changed. `test_fast_path_coverage_incomplete_for_non_ascii_question`'s
  own asserted behavior for its one Chinese example question changes accordingly; the commit that
  changes it must reference this spec, per this repo's own stop-condition convention for changing
  an existing test's asserted behavior.
- "板"/"面板"/"构件" stay unaliased — a future spec that wants to cover them must design their
  disambiguation explicitly, not inherit an accidental default from this one.

## Acceptance criteria

- Each Chinese entity/keyword combination in §B-C, exercised through `heuristic_multi_plan` and/or
  `heuristic_plan` directly, returns a `complete`-status IFC plan with the correct `entity_type`
  and `operation`, with zero model calls end-to-end through `AgentService.invoke()` against a real
  project.
- `fast_path_coverage` reports `coverage_status == "complete"` and the audit trail's
  `planning_mode` reads `"heuristic"` for the same set of questions.
- A non-Chinese, non-ASCII question (e.g. Cyrillic or Arabic text) continues to report
  `coverage_status == "incomplete"` and continues to reach the LLM planner — proven by a test, not
  assumed from the code reading alone.
- "这张图里的板有多少"/"图里的板有多少" continue to produce the existing ambiguity clarification,
  unaffected by this spec (bare "板" was never aliased).
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests
  scripts`, and `(cd apps/web && npm run build)` all clean after each lettered subsection.

## Documentation requirements

- `docs/decisions/README.md` D-057, including OD-49's resolution and the three excluded-term
  collision risks (板/面板/构件) named explicitly so a future spec doesn't reintroduce them
  unknowingly.
- `PROJECT_STATE.md` M14 milestone entry.

## Git / stop conditions

- Branch `feat/m14-chinese-fast-path`; one commit per lettered subsection (A-E).
- Stop and report rather than proceeding on own judgment if: a Chinese term in §B/§C turns out to
  collide with an existing string literal elsewhere in the codebase in a way not already surfaced
  in this spec's Rationale (the "板"/"面板"/"构件" collisions above were found by deliberately
  grepping for each candidate term before adding it — the same check should be repeated for any
  term added during implementation that this spec did not already enumerate); or if a genuinely
  ambiguous Chinese phrase (matches more than one entity alias, or an entity alias and a
  count/grouping keyword simultaneously in a way that changes meaning) is found that this spec's
  term list does not already resolve correctly.

## Owner decisions

- **OD-49**: knowingly reversing `fast_path_coverage`'s own documented "any non-ASCII request
  goes to the semantic planner" invariant, for the specific, enumerated Chinese count/group-by
  surface this spec defines (§B-D) — not a general "Chinese is now ASCII-equivalent" policy change.
  Any Chinese question outside this enumerated surface (quantity extrema, space distance, PDF/
  electrical domain, anything using an excluded alias like "构件") still correctly falls through
  to the LLM planner exactly as it does today.
