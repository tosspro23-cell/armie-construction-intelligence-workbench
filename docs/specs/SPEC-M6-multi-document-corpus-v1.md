# SPEC-M6 — Multi-document corpus (Phase 3 prerequisite)

## Objective

Replace the system's hardcoded single-PDF assumption with a genuine, moderately-sized
multi-document corpus (~15-20 synthetic documents across 3-4 document types), a
document-scoped `QueryPlan`, and a naive, deterministic document-selection baseline —
then demonstrate, with evidence, two distinct ways that baseline is insufficient. This
is deliberately **not** Azure AI Search — it is the prerequisite fixture and plumbing
that make AI Search a real, falsifiable requirement instead of a speculative one, per
`PROJECT_STATE.md`'s 2026-09-10 Phase 3 scoping note, revised after owner review below.

## Rationale

**Revision note (owner review, 2026-09-10):** the first draft of this spec scoped the
corpus at one new document and one contrived field-name collision. The owner correctly
rejected this as insufficient: a two-document collision only proves "a document-
selection step is architecturally necessary," which does not by itself justify
semantic/vector retrieval over a much simpler alternative (a rule-based document
router, or expanding the existing keyword list). Azure AI Search's value proposition
over keyword matching only becomes real when (a) the corpus has enough documents and
enough document-type diversity that hand-written keyword rules genuinely stop scaling,
and (b) at least one realistic question is phrased differently from the target
document's own vocabulary, so semantic similarity — not lexical overlap — is what
would actually find the answer. Neither condition holds at n=2. This revision targets
both.

Every document-lookup path in this codebase still assumes exactly one PDF exists:
`Settings.pdf_file`/`pdf_path` are singular, `ServiceContainer` constructs exactly one
`DocumentAnalyzer`, and `QueryPlan` has no field naming *which* document a question
targets (all verified current-state facts, unchanged from the first draft — see below).

This milestone's job is narrower than "build enterprise retrieval": build a corpus and
a naive baseline large and diverse enough to produce **two distinct, evidenced failure
modes**, not one:

1. **Precision failure (ambiguity):** the same field/quantity is answerable from more
   than one document under the same or similar wording — a keyword/field-name match
   finds multiple plausible candidates and cannot rank between them.
2. **Recall failure (vocabulary mismatch):** the answer to a realistically-phrased
   question exists in the corpus, but under different wording than the question uses
   (e.g. a narrative RFI response describing a load change in prose, not as a
   `"connected_load"`-labelled table cell) — no keyword-based match fires at all, even
   though a human reading the corpus would find it immediately.

Failure mode 2 is the stronger, more honest justification for semantic retrieval
specifically: no amount of additional keyword rules fixes a vocabulary mismatch,
because the gap is semantic, not lexical. Failure mode 1 alone (the first draft's
scope) only motivates *some* disambiguation step, which a simpler rule-based router
could also address. Recording both, with an honest, zero-model-call failure response
in each case (not a guess), is what gives SPEC-M7 (Azure AI Search) a real,
measurable bar to clear — "does semantic retrieval resolve failure modes 1 and 2 that
the naive baseline provably cannot" — instead of an assumed one.

**Explicit scale honesty (D-004 discipline carried into fixture design, not just
answers):** ~15-20 documents remains a *mechanism demonstration*, not an enterprise-
scale validation. This spec's documentation requirements (below) require stating that
distinction explicitly wherever this corpus's results are reported, the same way
SPEC-M3 states "single-environment... not production-hardened" rather than letting a
successful demo imply more than it proved.

**Design constraint carried forward from M1/M2P1/D-009, restated for this milestone's
benefit:** the naive baseline built here, and any AI Search that replaces it later,
may only ever perform document/passage *location* ("which document, and roughly
where, is the answer probably on"). It must never itself synthesize the answer — the
existing, tested `DocumentAnalyzer.native_lookup` deterministic extractor stays the
only thing that reads an actual field value, on whichever document the location step
selects. This is a hard boundary, not a style preference: it is what keeps D-004's
honest-failure/verification invariants intact once a probabilistic retrieval step
exists anywhere in the pipeline.

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
  fixture's specific clean, left-aligned tabular layout. A new *tabular* document
  reusing the same generator/layout conventions stays inside that characterization; a
  narrative document (RFI entries, meeting minutes) is not tabular at all and is
  explicitly not required to satisfy D-009's characterization — see §D below, "answerable"
  vs. "distractor" document types.

## Allowed scope

**A. Config and `ServiceContainer`: `pdf_file` (singular) → `pdf_files` (ordered list).**
- `Settings.pdf_files: list[str] = ["armie_demo_schedule.pdf"]` replaces `pdf_file`
  (default preserves exactly today's single-document behaviour for any deployment
  that doesn't override it — no silent behaviour change for an unmodified `.env`).
- `ServiceContainer` builds one `DocumentAnalyzer` per configured file, keyed by
  filename, held in an ordered `dict[str, DocumentAnalyzer]`. Iteration order is the
  configured list order — deterministic, not filesystem/glob order (a glob's order is
  platform-dependent, which would make the fixture's own regression tests
  non-reproducible).

**B. `QueryPlan.requested_document: str | None = None` (new field).**
- `None` means "search every configured document" (today's implicit behaviour, and
  the only behaviour possible with one document).
- Set only via `source_preference` (mirroring the existing manual-override pattern at
  `router.py:291`) or a future document-location step (SPEC-M7) — this milestone does
  not add natural-language document-name detection to the heuristic or semantic
  planner; that is exactly the capability SPEC-M7 exists to evaluate.

**C. Naive multi-document lookup in `_execute_pdf` (`apps/api/app/agent/graph.py`),
covering both failure modes.**
- When `requested_document` is set: unchanged behaviour, scoped to that one document.
- When unset: run `native_lookup` against every configured *tabular/answerable*
  document (§D) for the requested field, deterministically, zero model calls.
  - Exactly one document produces a matching record → answer from it exactly as
    today.
  - Zero documents match by field/keyword → today's existing
    `no_matching_record`/miss-reason path (D-010's `clarification_required`, zero
    model calls) — **this is also the honest response to failure mode 2** (a
    narrative document contains the answer in different words, but this milestone's
    naive baseline has no way to find it — it must say so, not guess, and must not
    silently skip narrative documents without recording that it did).
  - More than one document produces a matching record for the same field →
    `clarification_required`, naming the candidate documents, zero model calls
    (failure mode 1, OD-32, unchanged from the first draft).
- `Evidence`/citation locators gain a `document` field (source filename) so a
  multi-document answer's citation says which document it came from.

**D. Fixture: ~15-20 documents across answerable and distractor types.**
- **Answerable/tabular types** (must satisfy D-009's native_lookup characterization —
  reuse the existing generator's table-layout conventions): electrical/MEP schedule
  variants (multiple panels/levels, extending the existing generator function
  parametrically) and door/window spec sheets (a distinct field vocabulary — e.g.
  "Fire Rating", "Frame Material" — for corpus diversity, not just repeated
  schedules). Together, enough of these to host **one genuine cross-document field
  collision** (failure mode 1, e.g. the same field name appearing for two different
  panels/levels).
- **Distractor/narrative types** (not required to be `native_lookup`-extractable —
  see Verified current-state assumptions): RFI log entries and meeting-minute
  excerpts, mostly plausible noise (mentioning electrical/schedule topics without
  being the authoritative source), except **one deliberately planted fact** stated in
  prose, in different vocabulary than the schedules use (failure mode 2 — e.g. an RFI
  resolution describing a panel's capacity being revised, phrased without using the
  schedule's own column-header vocabulary at all).
- Total count and type mix (~15-20 documents, 3-4 types) is this spec's target;
  exact per-type counts are an implementation-time judgment call, not re-litigated
  document-by-document here.

**E. Tests.**
- Regression: every existing PDF-lookup test still passes unmodified with
  `pdf_files` defaulted to the single original document.
- New (failure mode 1): a fixture subset where a field exists in two answerable
  documents → asserts `disposition == "clarification_required"`, `fake.calls == []`,
  both candidate documents named.
- New (failure mode 2): a fixture subset where a realistically-phrased question's
  answer exists only in a narrative distractor document, in different wording than
  any table's field vocabulary → asserts the naive baseline's honest miss
  (`clarification_required`/`no_matching_record`, zero model calls) — this is the
  test that **is expected to demonstrate the gap**, not a passing "found it" case;
  its assertion is that the system fails safely, not that it succeeds. This is the
  recorded evidence SPEC-M7 must be measured against.
- New: a fixture subset where a field exists in exactly one answerable document
  (among several configured) → asserts a correct `answered` disposition with a
  citation naming that document (proves the single-match path still works across a
  larger corpus, not just within one document).
- New: `ServiceContainer` constructs one `DocumentAnalyzer` per configured
  *answerable* file, keyed correctly, for a `pdf_files` list of length > 1.

## Explicitly excluded scope

- **Azure AI Search, any vector index, any embeddings** — this milestone builds the
  fixture and the naive baseline that AI Search will need to outperform on both
  recorded failure modes; it contains no retrieval-by-meaning of any kind. That is
  SPEC-M7, gated on this milestone's evidenced failure modes actually existing.
- **ADLS Gen2 / any Azure storage change** — the expanded corpus stays in `demo_data/`
  like today's fixture; cloud document storage is SPEC-M7's concern (folded together
  with the still-open evidence-persistence gap per `PROJECT_STATE.md`'s scoping note).
- **Natural-language document-name detection** (the semantic or heuristic planner
  inferring *which* document from question wording alone) — `requested_document` is
  settable only via explicit `source_preference` in this milestone; inferring it from
  free text is exactly the retrieval capability SPEC-M7 exists to evaluate.
- **Extraction generality for narrative documents** — RFI/meeting-minute distractor
  documents are never required to pass through `native_lookup`; this milestone does
  not build prose/NLP extraction of any kind. If SPEC-M7's retrieval step later
  *locates* the right passage in one, reading it back out is a separate, later scope
  decision, not implied here.
- **IFC-side multi-model support** — this milestone is PDF-only.
- **Reconciliation (OD-15/D-011) across the new documents** — stays scoped to the
  original schedule only.
- **Resolving the existing `generate_demo_data.py` drift** (`REVIEW_REQUIRED.md`) from
  the first schedule/IFC — out of scope, tracked separately.
- **Enterprise-scale validation claims** — this corpus proves the two failure modes
  exist at a demonstration scale; it does not and must not be documented as proving
  what happens at real enterprise document volumes (hundreds to thousands of
  documents), which raises different concerns (index freshness, access control per
  document, cost) this milestone does not address.

## Affected surfaces

`apps/api/app/config.py`, `apps/api/app/services.py`, `apps/api/app/schemas/models.py`
(`QueryPlan.requested_document`, `Evidence` locator), `apps/api/app/agent/graph.py`
(`_execute_pdf`), `apps/api/app/agent/router.py` (only to thread `requested_document`
from `source_preference`, not to add document-name inference), `scripts/
generate_demo_data.py` (new parametrized generator functions for schedule variants,
spec sheets, RFI entries, meeting minutes), `demo_data/` (new fixture files), `.env.example`,
new/updated tests.

## Invariants

All prior invariants unchanged. New:
- A `source="pdf"` plan with no `requested_document` set, against a corpus where a
  field collides across more than one answerable document, must resolve to
  `clarification_required` at zero model calls — never a guessed answer, never a
  vision-model escalation (vision cannot resolve "which document," only "what's on
  this page").
- A realistically-phrased question whose answer exists only in a narrative distractor
  document must also resolve to an honest zero-model-call miss, never a fabricated
  answer and never a silent success claim — the naive baseline is expected to fail
  this case, and that failure must be recorded as such (not masked by, e.g.,
  incidentally routing to vision and having vision guess correctly by luck).

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including all new tests, with
  every pre-existing PDF-lookup test unmodified and still passing.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes.
- Manual verification (not just tests): run both a collision question and the
  vocabulary-mismatch question against the real API and confirm the actual responses
  match what the tests assert — this project's standing "verify, don't trust the test
  suite alone" discipline for anything touching disposition logic.
- The failure-mode-2 test must be shown to fail (i.e. the system would have to
  "succeed" for the wrong reason) if the distractor document's planted fact reused
  the schedule's own field vocabulary instead of paraphrasing it — confirming the
  test actually exercises a vocabulary gap and not an accidental keyword hit.

## Documentation requirements

`docs/specs/SPEC-M6-multi-document-corpus-v1.md` (this file, revised in place after
owner review — the revision note in Rationale stays, it is not scrubbed from history).
`D-017` (architecture decision record) recording the naive-baseline design, the
two-failure-mode structure, and why it deliberately does not attempt document-name
inference or narrative extraction. `PROJECT_STATE.md` M6 milestone entry, explicitly
stating the "mechanism demonstration, not enterprise-scale validation" caveat.
`REVIEW_REQUIRED.md`: note that the Phase 3 scoping note's prerequisite is now
satisfied only if both failure modes are actually evidenced (see stop conditions).

## Git / stop conditions

One commit per subsection (A–E), spec committed alone first (this revision, before
any code). Branch: `feat/m6-multi-document-corpus`. Stop and report rather than
proceeding if:
- The new tabular documents cannot be made to collide without also changing the
  *existing* schedule (which SPEC-M2's Tag/reconciliation work already depends on) —
  a real cross-milestone conflict, not a minor detail.
- `native_lookup`'s layout characterization does not hold for a new tabular document
  once generated — that would mean this milestone has silently grown into a
  document-extraction-generality milestone, out of scope per D-009.
- The failure-mode-2 fixture cannot be constructed such that a genuine vocabulary gap
  exists (i.e. every attempt at paraphrasing still accidentally shares enough
  vocabulary with the table headers that the naive baseline succeeds anyway) — report
  back rather than quietly weakening the test's intent to make it pass.

## Owner decisions

- **OD-30 (revised, owner-confirmed 2026-09-10).** Corpus scale: ~15-20 documents
  across 3-4 document types (schedule variants, spec sheets, RFI log entries, meeting
  minutes) — a mechanism-demonstration scale, explicitly documented as such, not an
  enterprise-scale claim. Supersedes the first draft's "one new document."
- **OD-31 (revised, needs owner confirmation).** Two failure-mode scenarios, not one:
  (1) a field collision between two answerable/tabular documents (precision failure);
  (2) a realistically-phrased question whose answer exists only in a narrative
  distractor document under different vocabulary than any table uses (recall
  failure). Recommend building both into the same corpus rather than two separate
  fixtures, since a real retrieval system has to handle both simultaneously.
- **OD-32 (owner-confirmed, unchanged from first draft).** On a cross-document
  collision with no `requested_document` set: `clarification_required` naming both
  candidates, zero model calls — never a guess, never vision escalation.
- **OD-33 (new, needs owner confirmation).** On a recall failure (vocabulary
  mismatch, no keyword match anywhere despite an answer existing): reuse the existing
  `clarification_required`/`no_matching_record` miss-reason path (D-010) rather than
  inventing a new disposition or failure category — this keeps the honest-failure
  contract's surface area unchanged; the corpus just makes an existing failure path
  reachable in a new, realistic way.
