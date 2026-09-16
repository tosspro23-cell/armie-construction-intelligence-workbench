# SPEC-M15 — Chinese fast path, phase 2: quantity extrema, reconciliation, distance, viewer-snapshot, and broader generalization

## Objective

Extend SPEC-M14's Chinese fast path from count/group-by to the rest of the operations English's
own deterministic fast path already covers — quantity extrema (最高/最大 etc.), space distance,
viewer-snapshot phrases, and `get_properties` — and broaden the Chinese reconciliation ("核对")
intent detection and the M14 count/group-by keyword sets themselves with additional natural
phrasings, so recognition is not limited to the exact terms M14 happened to enumerate first.

## Rationale

Owner-asked, 2026-09-16, immediately after M14 shipped: cover the remaining functionality (named
explicitly: 计算 — calculations/quantity extrema — and 核对 — reconciliation) and broaden Chinese
recognition's generalization ability generally, not just the M14 count/group-by surface.

**Confirmed by direct testing, not assumed, what already works and what does not.** Reconciliation
intent detection (`cross_source_reconciliation_requested`) already recognizes several natural
Chinese phrasings ("核对一下门和窗和图纸是否一致", "检查窗户与图纸排程是否一致") — it predates M14.
But `"门的数量和图纸是否匹配"` (a very natural way to ask this) is **not** detected: the verb marker
list has `不匹配` (mismatch) but not the bare positive `匹配` (match) — a real, narrow gap, not a
missing feature. Quantity extrema, space distance, viewer-snapshot phrases, and standalone
(non-contextual) `get_properties` have **zero** Chinese support today — confirmed by direct testing
(`这个项目最高的门是哪个？`, `门的最大高度是多少？`, `从卧室到厨房的距离是多少？`, `这个视图里能看到
什么？` all return `unsupported`/`partial`), matching M14's own "Explicitly excluded scope," which
named exactly these as deferred, not as already covered.

**A real design choice, not an oversight: quantity extrema gets a genuinely more general Chinese
implementation than English's own.** English's `quantity_match`/`paraphrase_quantity`
(`router.py`) are rigid `re.fullmatch` sentence templates, each enumerating a fixed, narrow entity
set (`doors|windows|walls|slabs|stairs` — missing space/roof/column/beam/member/railing/covering/
furniture/footing/plate even in English). Chinese phrasing has no equivalent fixed word order
("最高的门是哪个" / "门的最大高度是多少" / "门的高度最大值是多少" all mean the same thing, in three
different orders) — replicating English's template-matching approach would require enumerating
every phrasing combinatorially. Verified directly against 6 natural phrasings, a simpler,
genuinely more general design instead **decomposes** the question into three independent,
order-free signals — an entity alias (the full M14 table, not a hand-picked subset), a measure cue
(高度/宽度/长度/面积, or the height-implying shortcuts 最高/最矮 that need no explicit measure noun,
mirroring English's own "tallest"/"shortest" shortcut), and a direction cue (最大/最大值 vs.
最小/最小值/最低/最短) — and combines whichever of them are present, in any order. This is not
scope creep relative to English (which stays untouched here); it is what "broaden the
generalization ability" concretely means for phrasing patterns Chinese permits and English's own
fixed word order does not.

## Verified current-state assumptions

- `capability_gate`/`SUPPORTED_ENTITY_TYPES` need no change — every entity type this spec's
  quantity-extrema detection can resolve already has a canonical `Ifc*` mapping via M14's
  `ELEMENT_ALIASES`.
- `average`/`sum` remain outside fast-path completeness in **both** languages (`heuristic_plan`'s
  own `complete` formula never accepts them ungrouped) — this spec adds no Chinese support for
  them, matching M14's own precedent exactly, not a new gap.
- `get_properties` **is** already part of English's fast-path completeness
  (`operation == "get_properties"` is one of `complete`'s three accepted conditions) — its
  Chinese gap (no "属性" keyword in `heuristic_plan`'s own operation-detection chain, as opposed
  to the already-Chinese-aware *contextual* deictic "这个构件有什么属性" path, a separate
  mechanism) is therefore a real completeness gap this spec closes, not new scope beyond parity.
- `AgentService._route`'s dispatch order (verified in SPEC-M14's own Rationale) means a *complete*
  single-entity `heuristic_plan` result reaches the fast path via `use_fast_path`'s
  `plan.match_status == "complete" and plan.source == "ifc"` clause exactly as an English one does
  — no `graph.py` change is needed for quantity extrema/get_properties/distance, only for
  viewer-snapshot (see §D, which needs `has_viewer_context` exactly as English's own phrase does).

## Allowed scope

### A. Chinese quantity-extrema detection, decomposed rather than templated (own commit)

A new branch in `heuristic_plan`, before the `recognises_ifc` fallback: a measure-word map
(高度→height, 宽度→width, 长度→length, 面积→area) and two direction-word sets (最大/最大值 → max,
最小/最小值/最低/最短 → min), plus two measure-implying shortcuts (最高 → max+height, 最矮 →
min+height, mirroring English's own tallest/shortest). Fires only when an `ELEMENT_ALIASES` entity
is also present (via `_alias_search`, so the full table — not English's narrower fixed set) and
the outcome fully resolves both a measure and a direction — a partial match (only one of the two
present) falls through to the semantic planner exactly like an incomplete English phrasing would.

### B. Reconciliation ("核对") phrase broadening (own commit)

`cross_source_reconciliation_requested`'s `verb_marker` gains bare `匹配` (match) alongside the
existing `不匹配` (mismatch) — the one concrete gap found in Rationale. No other change to this
function; its entity/drawing markers already cover the natural phrasings tested.

### C. Chinese space-distance detection (own commit)

A new Chinese distance pattern in `heuristic_plan`, `r"从?(.+?)到(.+?)(?:的距离|有多远|距离)"` (verified against 4 natural phrasings including with/without the leading "从"), feeding the
existing `space_distance` `QueryPlan` construction identically to the English `distance_match`
branch beside it — same ambiguity-requires-clarification behavior downstream, unchanged.

### D. Chinese viewer-snapshot phrases (own commit)

Chinese alternatives added to the existing English viewer-snapshot phrase check: 当前视图,
现在这个视图, 这个视角, 看到什么, 可见 — same `has_viewer_context`-gated `match_status`
(`"complete"` only when a viewer selection/snapshot is actually active, `"partial"` otherwise,
exactly like the English phrase beside it).

### E. `get_properties` Chinese keyword (own commit)

`heuristic_plan`'s own operation-detection chain gains `"属性"` alongside `("property",
"properties")` — closes the one real completeness gap named in Verified current-state
assumptions.

### F. Broaden M14's own count/group-by surface (own commit)

A modest, individually-justified set of additional natural phrasings — not an exhaustive synonym
sweep:

- Count intent: `有几` (a very common colloquial opener — "有几扇门"/"有几个空间"/"有几道墙" — bare
  substring, matches regardless of the measure word that follows), `总共`, `总数`, `总计`.
- Storey grouping: `各层`, `逐层`, `每个楼层`.
- Entity aliases: `门扇` → `IfcDoor` (door leaf), `窗子` → `IfcWindow` (colloquial), `地基` →
  `IfcFooting` (colloquial "foundation," alongside the existing formal `基础`).

Each addition is checked against the same collision risks SPEC-M14's own Rationale already
established (bare "板"/"面板"/"构件" stay excluded; none of these new terms overlap them).

### G. Tests (own commit)

- Chinese-parity parametrized cases for each of §A-F, mirroring SPEC-M14's own test style: each
  quantity-extrema phrasing variant, the reconciliation "匹配" gap (a genuine pre-fix failure,
  verified the same way SPEC-M14's own tests were), the distance pattern, viewer-snapshot phrases
  (with and without `has_viewer_context`), the `get_properties` keyword, and each §F addition.
- A live end-to-end `AgentService.invoke()` case for at least the quantity-extrema path (the
  operation with the most novel matching logic), mirroring SPEC-M14 §E's own harness.

## Explicitly excluded scope

- **No PDF/electrical-schedule domain detection in Chinese** (the `("load", "circuit",
  "diversity", "schedule", "breaker", "electrical")` English-only gate in `heuristic_plan`) — a
  Chinese electrical-schedule question already correctly falls through to the LLM planner today
  (unaffected, not broken), and this domain crosses into PDF-specific vocabulary this spec's
  IFC-focused scope does not cover.
- **No `average`/`sum` Chinese support** — neither operation is part of English's own fast-path
  completeness either (Verified current-state assumptions); adding Chinese for an operation
  English itself does not fast-path would be a new asymmetry, not parity.
- **No exhaustive Chinese synonym dictionary.** §F adds a small, individually-justified set; a
  future request for specific additional phrasings should extend this list deliberately, the same
  way this spec and M14 did, not attempt to enumerate Chinese exhaustively in one pass.
- **No change to `reconciliation_plan`'s own subplan construction, or to OD-15's door/window-only
  reconciliation scope.** §B only fixes one verb-detection gap in the existing intent guard.

## Affected surfaces

- `apps/api/app/agent/router.py` — `cross_source_reconciliation_requested` (§B), `heuristic_plan`
  (§A, C, D, E, F's keyword additions), `ELEMENT_ALIASES` (§F's three new aliases).
- `tests/test_router_contract.py` — new Chinese-parity cases for §A-F.
- `docs/decisions/README.md` — new `D-058` entry.
- `PROJECT_STATE.md` — `M15` milestone entry.

No change to `graph.py`, `plan_validation.py`, response-formatting (already Chinese-aware, D-055),
`fast_path_coverage` (unaffected — §A/C/D/E all reach the fast path via `heuristic_plan`'s own
`match_status`, per Verified current-state assumptions, the same mechanism M14 already
established for `heuristic_multi_plan`), or any frontend file.

## Invariants

- Zero new model calls for any question this spec makes deterministic.
- English behavior is unchanged everywhere: every new Chinese branch/keyword is additive
  (`or`-ed alongside existing English checks, or a wholly separate branch that only fires when its
  own Chinese-specific pattern matches).
- Quantity-extrema detection only fires when *both* a measure and a direction resolve — an
  ambiguous or partial Chinese phrasing (e.g., only a direction word, no entity) falls through to
  the semantic planner exactly as an incomplete English phrasing already does, never a guessed
  plan.
- Every §F addition is checked against SPEC-M14's own documented collision risks (板/面板/构件)
  before being added, matching that spec's own stop-condition discipline.

## Acceptance criteria

- Each phrasing in §A-F, exercised through `heuristic_plan` (and, for §A, through
  `AgentService.invoke()` end-to-end), returns a `complete`-status plan with the correct
  `entity_type`/`operation`/`measure`/`aggregation` (or `filters` for distance), zero model calls.
- The reconciliation "匹配" gap (§B) is proven to genuinely fail pre-fix, matching this session's
  established verification standard.
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests
  scripts`, and `(cd apps/web && npm run build)` all clean after each lettered subsection.

## Documentation requirements

- `docs/decisions/README.md` D-058.
- `PROJECT_STATE.md` M15 milestone entry.

## Git / stop conditions

- Branch `feat/m15-chinese-fast-path-phase2`; one commit per lettered subsection (A-G).
- Stop and report rather than proceeding on own judgment if: a §F candidate term is found to
  collide with an existing string literal elsewhere in the codebase (the same check SPEC-M14's own
  Rationale performed for 板/面板/构件, repeated here for each new term before adding it); or if a
  quantity-extrema phrasing is found where the decomposed measure/direction signals resolve to a
  *wrong* combination (for example, a phrasing where "最矮" is not actually being used to mean
  "shortest in height" in context) — that would mean the decomposition itself needs a narrower
  design, not just a term-list fix.

## Owner decisions

None beyond OD-49 (SPEC-M14), which already covers the general principle this spec extends
(narrow, enumerated Chinese fast-path surfaces, not a blanket non-ASCII exemption). No new
capability-boundary question is opened here — every operation this spec adds Chinese support for
already exists and is already fast-pathed in English.
