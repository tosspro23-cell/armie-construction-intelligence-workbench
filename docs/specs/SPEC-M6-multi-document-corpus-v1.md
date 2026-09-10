# SPEC-M6 — Multi-document corpus (Phase 3 prerequisite)

## Objective

Replace the system's hardcoded single-PDF assumption with a genuine multi-document
corpus (several synthetic schedules), a document-scoped `QueryPlan`, and a naive,
deterministic document-selection baseline. This is deliberately **not** Azure AI
Search — it is the prerequisite fixture and plumbing that make AI Search a real,
falsifiable requirement instead of a speculative one, per `PROJECT_STATE.md`'s
2026-09-10 Phase 3 scoping note.

## Rationale

Every document-lookup path in this codebase assumes exactly one PDF exists:
`Settings.pdf_file`/`pdf_path` are singular, `ServiceContainer` constructs exactly
one `DocumentAnalyzer`, and `QueryPlan` has no field naming *which* document a
question targets. "Enterprise retrieval" (the originally-planned Phase 3) cannot be
evaluated against this: a corpus of one document gives Azure AI Search nothing to
retrieve *among*, so there would be no way to verify semantic search actually beats
what already exists — the same verifiability standard that drove M1/M2P1 to replace
probabilistic vision extraction with deterministic native-text extraction in the
first place (`docs/decisions/README.md` D-009).

This milestone's job is narrower than "build enterprise retrieval": add enough real
documents, with at least one genuine cross-document ambiguity, that a **naive
deterministic baseline** (try the field against every configured document; answer if
exactly one matches, ask for clarification if more than one does) is demonstrably
insufficient for at least one realistic question shape. That insufficiency is the
actual, evidenced justification for SPEC-M7 (Azure AI Search / semantic retrieval),
not an assumption. Until that gap is shown to exist, AI Search work has no
falsifiable acceptance criterion.

**Design constraint carried forward from M1/M2P1/D-009, restated for this
milestone's benefit:** the naive baseline built here, and any AI Search that
replaces it later, may only ever perform document *location* ("which
document/page is the answer probably on"). It must never itself synthesize the
answer — the existing, tested `DocumentAnalyzer.native_lookup` deterministic
extractor stays the only thing that reads an actual field value, on whichever
document the location step selects. This is a hard boundary, not a style
preference: it is what keeps D-004's honest-failure/verification invariants intact
once a probabilistic retrieval step exists anywhere in the pipeline.

## Verified current-state assumptions

- `apps/api/app/config.py:39-40,104-105` — `pdf_file: str = "armie_demo_schedule.pdf"`
  and `pdf_path` are singular; nothing in this codebase currently expresses "more
  than one document."
- `apps/api/app/services.py:41-44` — `ServiceContainer` constructs exactly one
  `DocumentAnalyzer(pdf_path=settings.pdf_path, ...)`.
- `apps/api/app/schemas/models.py`'s `QueryPlan` (line 112) has no field identifying
  which document a `source="pdf"` plan targets — grep-verified, no `document`/
  `document_id`/`source_document` field exists anywhere in this model.
- `apps/api/app/agent/router.py:311` — the PDF-domain heuristic fast path is a flat
  English keyword list (`"load", "circuit", "diversity", "schedule", "breaker",
  "electrical"`), already tracked as English-only in `REVIEW_REQUIRED.md`; it has no
  concept of routing to a *specific* document because there has only ever been one.
- `demo_data/` contains exactly one PDF (`armie_demo_schedule.pdf`, 2 pages per
  SPEC-M2's second-page addition) and one IFC (`armie_demo.ifc`). `scripts/
  generate_demo_data.py` already has documented, accepted drift from the committed
  PDF/IFC fixtures (`REVIEW_REQUIRED.md`'s "M2: `scripts/generate_demo_data.py` no
  longer reproduces the committed fixtures byte-for-byte") — this milestone does not
  need to resolve that drift, only avoid making it worse for whatever it adds.
- `DocumentAnalyzer.native_lookup` (per D-009) is characterized against this
  fixture's specific clean, left-aligned layout — a new document that reuses the
  same generator/layout conventions stays inside that characterization; a document
  with a materially different layout would not, and is out of this milestone's scope.

## Allowed scope

**A. Config and `ServiceContainer`: `pdf_file` (singular) → `pdf_files` (ordered list).**
- `Settings.pdf_files: list[str] = ["armie_demo_schedule.pdf"]` replaces `pdf_file`
  (default preserves exactly today's single-document behaviour for any deployment
  that doesn't override it — no silent behaviour change for an unmodified `.env`).
- `ServiceContainer` builds one `DocumentAnalyzer` per configured file, keyed by
  filename, held in an ordered `dict[str, DocumentAnalyzer]`. Iteration order is the
  configured list order — deterministic, not filesystem/glob order (a glob's order
  is platform-dependent, which would make the fixture's own regression tests
  non-reproducible).

**B. `QueryPlan.requested_document: str | None = None` (new field).**
- `None` means "search every configured document" (today's implicit behaviour, and
  the only behaviour possible with one document).
- Set only via `source_preference` (mirroring the existing manual-override pattern
  at `router.py:291`) or a future document-location step (SPEC-M7) — this milestone
  does not add natural-language document-name detection to the heuristic or
  semantic planner; that is exactly the capability SPEC-M7 exists to evaluate.

**C. Naive multi-document lookup in `_execute_pdf` (`apps/api/app/agent/graph.py`).**
- When `requested_document` is set: unchanged behaviour, scoped to that one document
  (today's exact code path, now addressed explicitly instead of implicitly-the-only-
  one).
- When unset: run `native_lookup` against every configured document for the
  requested field, deterministically, zero model calls.
  - Exactly one document produces a matching record → answer from it exactly as
    today (this is what keeps every existing single-document test passing
    unmodified when only one document is configured, and what most questions
    against the expanded corpus should still do).
  - Zero documents match → today's existing `no_matching_record`/miss-reason path,
    unchanged.
  - **More than one document produces a matching record for the same field → the
    new case this milestone exists to create.** Disposition
    `clarification_required`, naming the candidate documents, zero model calls —
    not a guess, not a vision escalation. This is the naive baseline's honest
    failure mode, and it is what SPEC-M7 will need to demonstrably do better than.
- `Evidence`/citation locators gain a `document` field (source filename) so a
  multi-document answer's citation says which document it came from — previously
  implicit (there was only one), now must be explicit.

**D. Fixture: at least one new synthetic PDF creating a genuine collision.**
- Extend `scripts/generate_demo_data.py`'s existing schedule-generation function
  (or add a sibling one) to produce a second synthetic schedule document, reusing
  the same deterministic layout conventions as the first (native_lookup's
  characterization must still hold — see Verified current-state assumptions).
  Content constructed so that **at least one field name genuinely collides across
  two documents** (see OD-31 below for what field/scenario) — a fabricated
  ambiguity is the whole deliverable of this milestone, not incidental.
  Also at least one field that exists in only the new document (proving the
  single-match path still works across documents, not just within one).

**E. Tests.**
- Regression: every existing PDF-lookup test still passes unmodified with
  `pdf_files` defaulted to the single original document (proves this milestone
  changes nothing about today's behaviour when the corpus isn't actually expanded).
- New: a two-document fixture where a field exists in both → asserts
  `disposition == "clarification_required"`, `fake.calls == []` (zero model calls,
  matching the M1.5 zero-cost-refusal precedent), and both candidate documents named
  in the response.
- New: a two-document fixture where a field exists in only the second document →
  asserts a correct `answered` disposition with a citation naming that document.
- New: `ServiceContainer` constructs one `DocumentAnalyzer` per configured file,
  keyed correctly, for a `pdf_files` list of length > 1.

## Explicitly excluded scope

- **Azure AI Search, any vector index, any embeddings** — this milestone builds the
  fixture and the naive baseline that AI Search will need to outperform; it
  contains no retrieval-by-meaning of any kind. That is SPEC-M7, gated on this
  milestone's evidenced insufficiency case actually existing.
- **ADLS Gen2 / any Azure storage change** — the expanded corpus stays in `demo_data/`
  like today's fixture; cloud document storage is SPEC-M7's concern (folded together
  with the still-open evidence-persistence gap per `PROJECT_STATE.md`'s scoping note).
- **Natural-language document-name detection** (the semantic or heuristic planner
  inferring *which* document from question wording alone) — `requested_document`
  is settable only via explicit `source_preference` in this milestone; inferring it
  from free text is exactly the retrieval capability SPEC-M7 exists to evaluate.
- **IFC-side multi-model support** — this milestone is PDF-only; `IfcRepository`
  and its single-model assumption are untouched.
- **Reconciliation (OD-15/D-011) across the new documents** — the door/window
  IFC↔PDF reconciliation pilot stays scoped to the original schedule only; extending
  it to additional documents is a separate scope decision, not implied by this spec.
- **Resolving the existing `generate_demo_data.py` drift** (`REVIEW_REQUIRED.md`)
  from the first schedule/IFC — out of scope, tracked separately.

## Affected surfaces

`apps/api/app/config.py`, `apps/api/app/services.py`, `apps/api/app/schemas/models.py`
(`QueryPlan.requested_document`, `Evidence` locator), `apps/api/app/agent/graph.py`
(`_execute_pdf`), `apps/api/app/agent/router.py` (only to thread `requested_document`
from `source_preference`, not to add document-name inference), `scripts/
generate_demo_data.py`, `demo_data/` (new fixture file(s)), `.env.example`, new/
updated tests.

## Invariants

All prior invariants unchanged. New: a `source="pdf"` plan with no
`requested_document` set, against a corpus where a field collides across more than
one document, must resolve to `clarification_required` at zero model calls — never
to a guessed answer from either candidate, and never to a vision-model escalation
(vision cannot resolve "which document," only "what's on this page," so escalating
would not address the actual ambiguity — see Rationale's design constraint).

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including the new
  multi-document tests, with every pre-existing PDF-lookup test unmodified and
  still passing.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes (only relevant if a citation-rendering
  change is needed for the new `document` locator field — confirm during
  implementation whether the frontend needs updating at all, since `citationFacts`
  in `main.tsx` already renders arbitrary locator keys generically).
- Manual verification (not just tests): run a real question against the expanded
  fixture whose field collides across documents and confirm the API actually
  returns `clarification_required` naming both documents, mirroring this project's
  standing "verify, don't trust the test suite alone" discipline for anything
  touching disposition logic.

## Documentation requirements

New `docs/specs/SPEC-M6-multi-document-corpus-v1.md` (this file). `D-017`
(architecture decision record) recording the naive-baseline design and why it
deliberately does not attempt document-name inference. `PROJECT_STATE.md` M6
milestone entry. `REVIEW_REQUIRED.md`: note that the Phase 3 scoping note's
prerequisite is now satisfied (or not yet, if this milestone lands without a real
collision case — see stop conditions).

## Git / stop conditions

One commit per subsection (A–E), spec committed alone first. Branch:
`feat/m6-multi-document-corpus`. Stop and report rather than proceeding if: the new
document's content cannot be made to collide with the existing schedule without
also changing the *existing* document (which would touch the fixture SPEC-M2's
Tag/reconciliation work already depends on — a real cross-milestone conflict, not a
minor detail); or `native_lookup`'s layout characterization does not actually hold
for the new document once generated (i.e., it needs its own vision fallback or a
different extraction path) — that would mean this "prerequisite" milestone has
silently grown into a document-extraction-generality milestone, which is explicitly
out of scope per D-009.

## Owner decisions

- **OD-30 (needs owner confirmation).** Corpus size and shape for this milestone:
  keep the existing 2-page schedule, add **one** new synthetic schedule document
  (not multiple), sized just enough to create one genuine field collision plus one
  document-unique field. Recommended over a larger corpus: this milestone's job is
  to prove the insufficiency case exists at all, not to build a realistic
  enterprise-scale document set — that scale question belongs to SPEC-M7 once the
  mechanism is proven.
- **OD-31 (needs owner confirmation).** The colliding field: recommend reusing an
  existing field name from the current schedule (e.g. a `"connected_load"`-shaped
  field already exercised by existing tests) on a *different* board/panel in the
  new document, so the collision is a realistic "which panel's load did you mean
  across two schedules" question, not a contrived duplicate. Alternative: pick a
  wholly new field name shared by both documents instead of reusing an existing
  one, if the owner would rather not touch anything the current schedule's existing
  tests depend on.
- **OD-32 (needs owner confirmation).** On a cross-document collision with no
  `requested_document` set, this spec defaults to `clarification_required` naming
  both candidates (never a guess, never vision escalation — see Invariants).
  Confirm this is the desired behaviour rather than, e.g., answering with the first
  match and flagging the ambiguity in metadata only (which would be a weaker,
  D-004-adjacent-risk alternative not recommended here).
