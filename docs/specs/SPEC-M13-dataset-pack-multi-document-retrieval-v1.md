# SPEC-M13 — Multi-document corpus + AI Search retrieval for the real Dataset Pack buildings

## Objective

Give DigitalHub and Duplex (the two real, openly-licensed Dataset Pack buildings added this
session, D-036 through D-044) a real multi-document corpus and working Azure AI Search retrieval
of their own, mirroring what "demo" already has (SPEC-M6/M7) — then prove it with a real,
measured benchmark report against the live index, not narrative claims.

## Rationale

Today, `digitalhub`/`duplex` each have exactly one PDF (`digitalhub_schedule.pdf`/
`duplex_schedule.pdf`, real-tag-grounded door/window schedules, D-035/D-036/D-037) — no
multi-document corpus, and no retrieval demonstration against real building data at all.
`_retrieve_relevant_documents` (`apps/api/app/agent/graph.py`) is additionally hard-gated to
`project_id == "demo"` — an explicit, asserted exclusion from SPEC-M9, not an oversight:

```python
# SPEC-M9 §Explicitly excluded: retrieval is scoped to the "demo" project only
if state["project_resources"].manifest.project_id != "demo":
    return []
```

The owner asked directly for retrieval capability to be demonstrable against the two real
buildings' own real models and drawings, with "as many business scenarios as possible" and a
proper benchmark-style eval — this is not achievable without relaxing that gate, so this spec
proposes doing so explicitly, flagged as a new owner decision (OD-48) rather than silently
changed, per SPEC-M9's own framing of it as a deliberate boundary.

**Real architecture constraint, verified by reading the code, not assumed.** Azure AI Search here
is a **single, shared, non-project-scoped index** (`scripts/index_document_corpus.py`'s
`build_index`): the index schema has no `project_id` field, and documents are keyed by bare
filename basename only. Cross-project bleed is prevented today only because
`_retrieve_relevant_documents`'s caller filters retrieved names against the *current* project's
own `document_analyzers` dict (`relevant_configured = [(name, score) for name, score in retrieved
if name in analyzers]`) — a real, working safeguard, but one that silently depends on no two
projects ever sharing a basename. `demo`'s corpus already uses generic names
(`schedule_l2_east.pdf`, `rfi_log_047.pdf`); this spec's new documents must use project-prefixed
filenames (`digitalhub_...`, `duplex_...`) so this safeguard can never be defeated by an accidental
collision.

**Real per-storey/per-tag data confirmed before designing fixtures, not guessed:**

| | Duplex | DigitalHub |
|---|---|---|
| Real storeys | Level 1, Level 2, Roof, T/FDN | B01_OKRD, E00_OKRD, E01_OKRD |
| Doors per storey | Level 1: 6, Level 2: 8 | E00: 24, E01: 25, B01: 13 |
| Windows per storey | Level 1: 4, Level 2: 18, Roof: 2 | E00: 21, E01: 26 |
| Sample real door Tags | 146596, 146678, 150173, 150257, 150378 | 2421088, 2421089, 2421363, 2423930, 2423931 |
| Sample real window Tags | 145788, 146016, 146885, 147051, 147686 | 2529359, 2529899, 2530077, 2530175, 2530283 |

## Verified current-state assumptions

- `azure_search_endpoint`/`azure_search_relevance_threshold` (`apps/api/app/config.py`) are
  global `Settings` fields, not per-project — no new deploy parameter is needed once the index
  contains the new documents and the code gate is relaxed; the same already-configured
  `armiem3-search`/`text-embedding-3-small` resources serve every project.
- `ServiceContainer.get_project` resolves each project's own `document_analyzers` from that
  project's `projects_registry.json` `pdf_files` entry (SPEC-M9 §C) — adding documents there is
  the only registry-side change needed; `_retrieve_relevant_documents`'s existing
  `name in analyzers` filter needs no change to correctly scope results once the gate above is
  relaxed.
- `scripts/index_document_corpus.py`'s `full_corpus` list and `DOCUMENT_TYPES` prefix map are both
  hardcoded to the demo corpus only — extending indexing to the new documents means editing this
  script, not a new one (one shared index, one indexing entry point, per the file's own header).
- `DocumentAnalyzer._read_table`'s D-009 characterization (clean, left-aligned columns at fixed
  `x` positions) is what every new tabular document must match, exactly as `generate_demo_data.py`
  and `generate_duplex_schedule.py` already do via their own `_TABLE_X` constants — reused here,
  not re-derived.

## Allowed scope

### A. Real-tag-grounded multi-document corpus, one generator script per project (own commits)

`scripts/generate_digitalhub_corpus.py` and `scripts/generate_duplex_corpus.py` (mirroring
`generate_duplex_schedule.py`'s existing structure: read the real IFC directly via `ifcopenshell`,
write PDFs with `fitz`, never touch the existing `*_schedule.pdf`/reconciliation fixture). Each
produces 7 new documents for its project, prefixed to guarantee no basename collision in the
shared Search index:

- **Three distribution-schedule PDFs, one per real storey** (`{project}_schedule_<storey>.pdf`) —
  the same electrical-panel-schedule fixture *type* SPEC-M6 already validated for `demo` (a
  separate synthetic domain layered on top of the real IFC, not derived from it — `demo`'s own
  panel schedules aren't derived from its IFC either), reusing `_TABLE_X`'s exact column
  positions. A **precision-collision fixture**, mirroring SPEC-M6's own proven design exactly:
  one panel label repeated across two of the three documents with different connected-load
  values, titled and subtitled with the real building name and real storey name for grounding.
- **One door hardware spec sheet** (`{project}_door_spec_sheet.pdf`), referencing 3 real door Tags
  from that building's own IFC (not already altered by the reconciliation fixture) with a
  synthetic fire-rating/frame-material column pair.
- **One window glazing spec sheet** (`{project}_window_spec_sheet.pdf`), same pattern with 3 real
  window Tags.
- **One RFI log** (`{project}_rfi_log_001.pdf`, narrative, no table) — a **recall-failure
  fixture**, mirroring SPEC-M6's `rfi_log_047.pdf` design: describes a real, specific field
  concern tied to one real door/window Tag from that building, stating a value in prose that
  appears in no table anywhere in the corpus.
- **One meeting-minutes PDF** (`{project}_meeting_minutes_2026_02_10.pdf`, narrative, no table),
  referencing the real storey names for grounding.

`demo_data/projects_registry.json`'s `digitalhub`/`duplex` entries gain these 7 files each in
`pdf_files` and `files` (with real `content_sha256` hashes), leaving `demo`/`westgate` untouched.

### B. Relax the demo-only retrieval gate (own commit, OD-48)

`_retrieve_relevant_documents` (`apps/api/app/agent/graph.py`) drops the `project_id != "demo":
return []` early return — retrieval becomes available to any project whose
`azure_search_endpoint`/embedding provider are configured (unchanged: still returns `[]`
immediately if either is unset, so `demo`'s existing behavior when retrieval is off is
unaffected). No other logic in this method changes — the existing `name in analyzers` filter
already scopes results correctly per project, per Rationale above.

### C. Indexing (own commit, operator-run, not part of the API's own runtime)

`scripts/index_document_corpus.py`'s `full_corpus` and `DOCUMENT_TYPES` extended to include the 14
new documents (both projects' new corpora) alongside the existing 18 `demo` documents in the one
shared index — matching this script's own established "index everything in the fixture, regardless
of which project's `PDF_FILES` a given deployment happens to be running with" convention.

### D. Tests (own commit)

- `tests/test_ifc_evidence_sampling.py`-style regression tests are not the right shape here (this
  is document/retrieval behavior, not IFC geometry) — new tests in
  `tests/test_dataset_pack_corpus.py`: each new schedule document's table is readable via
  `DocumentAnalyzer._read_table` (real column geometry, not assumed); the precision-collision
  scenario for each project produces `clarification_required` naming both documents with
  `model_call_count == 0` (native lookup finds both hits directly, matching SPEC-M6's own
  call-count-verified precision-failure test).
- `tests/test_azure_ai_search_retrieval.py` gains a CI-safe (`FakeSearchClient`/
  `FakeEmbeddingProvider`, no live Azure call) test proving retrieval now actually runs for a
  non-`demo` project (a call-count assertion on the fake client, the same technique already used
  throughout this file) — the real, previously-untested claim this milestone's §B change makes.

### E. Live benchmark report (own commit, real data, not narrative)

`docs/reports/2026-09-16-m13-dataset-pack-retrieval-baseline.md`, following
`docs/reports/2026-09-10-m7-azure-ai-search-baseline.md`'s exact methodology and rigor standard:
real measured Azure AI Search scores (hybrid and vector-only, both projects) for each precision-
collision and recall-failure question, a real end-to-end `TestClient`/live-app request/response
pair for at least one recall-failure case per project, and — matching M7's own "honest nuance"
section — at least one paraphrased recall question per project that removes literal keyword
overlap, to isolate the genuinely-semantic (not just BM25-lexical) part of the claim.

## Explicitly excluded scope

- **No third real building or further Dataset Pack expansion.** Scoped to enriching the two
  buildings already added this session.
- **No change to the existing `digitalhub_schedule.pdf`/`duplex_schedule.pdf` door/window
  schedules or their planted reconciliation discrepancies** (D-036/D-037's own fixtures) — this
  milestone's new documents are additive, a separate corpus layered alongside them.
- **No per-project Azure AI Search index** (a second index, or a `project_id` field added to the
  existing schema). The single-shared-index-plus-filename-filter architecture already works
  correctly once filenames don't collide (verified above); redesigning it is a larger, separate
  change not justified by this milestone's actual need.
- **No change to `azure_search_relevance_threshold`** (`0.025`, set from real M7 baseline data) --
  reused as-is; this spec's own benchmark report (§E) will say plainly if the new data suggests it
  should move, rather than assuming it must.

## Affected surfaces

- `scripts/generate_digitalhub_corpus.py`, `scripts/generate_duplex_corpus.py` — new.
- `demo_data/projects/digitalhub/corpus/*.pdf`, `demo_data/projects/duplex/corpus/*.pdf` — new
  (7 files each).
- `demo_data/projects_registry.json` — `digitalhub`/`duplex` entries extended only.
- `apps/api/app/agent/graph.py` — `_retrieve_relevant_documents`'s project gate.
- `scripts/index_document_corpus.py` — corpus list extended.
- `tests/test_dataset_pack_corpus.py` — new; `tests/test_azure_ai_search_retrieval.py` — one new
  test.
- `docs/reports/2026-09-16-m13-dataset-pack-retrieval-baseline.md` — new.

`demo`/`westgate`'s own corpora, `digitalhub_schedule.pdf`/`duplex_schedule.pdf`, and
`azure_search_relevance_threshold` are unmodified.

## Invariants

- Zero new model calls on the common (non-retrieval) path — retrieval still only ever runs inside
  `_execute_pdf_multi_document`'s existing zero-hit branch, unchanged.
- Every new document is honestly labeled as an ARMIE-generated synthetic fixture layered on real
  IFC data, exactly like every existing Dataset Pack document (`digitalhub_schedule.pdf`'s own
  cover-page disclosure is the precedent) — never presented as a real project document.
- No cross-project document-name collision in the shared Search index (verified by construction:
  every new filename is project-prefixed).
- `demo`'s own retrieval behavior (still gated on `azure_search_endpoint` being configured) is
  unaffected by relaxing the project check.

## Acceptance criteria

- Both `digitalhub` and `duplex` have a 7-document corpus in their `projects_registry.json`
  entries, each document's table (where applicable) readable via `DocumentAnalyzer._read_table`.
- A precision-collision question against each project's own corpus returns
  `clarification_required` naming both colliding documents, `model_call_count == 0` — proven by a
  CI-safe test, not just the live report.
- A CI-safe test proves retrieval now actually executes (via a fake search client call-count
  assertion) for a non-`demo` project — the real claim §B's code change makes.
- The live benchmark report (§E) shows real measured scores for at least one precision-collision
  and one recall-failure question per project, plus one paraphrased/harder recall question per
  project isolating genuine semantic similarity from lexical overlap.
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests
  scripts`, and `(cd apps/web && npm run build)` all clean after each lettered subsection (no
  frontend change is expected, but the gate is run regardless per this repo's own standard
  practice).

## Documentation requirements

- `docs/decisions/README.md` D-051 (this milestone, including OD-48's resolution).
- `PROJECT_STATE.md` M13 milestone entry.
- Each new corpus document generator script documents its own precision-collision/recall-failure
  design inline, matching `generate_demo_data.py`/`generate_duplex_schedule.py`'s own
  documentation depth.

## Git / stop conditions

- Branch `feat/m13-dataset-pack-retrieval`; one commit per lettered subsection (A-E).
- Stop and report rather than proceeding on own judgment if: a real door/window Tag chosen for a
  spec-sheet/RFI fixture turns out to already be involved in the existing reconciliation
  discrepancies (D-036/D-037), which would make the new fixture's "correct" value ambiguous; or if
  indexing the new documents into the live shared Search index surfaces a real defect (a filename
  collision that slipped through, a table geometry mismatch) not anticipated here.

## Owner decisions

- **OD-48**: relaxing `_retrieve_relevant_documents`'s demo-only project gate (SPEC-M9's own
  explicit exclusion) to allow any project with retrieval configured. This is necessarily implied
  by the owner's own request (retrieval demonstrated against the real Dataset Pack buildings is
  not achievable without it) and is proceeded with on that basis, but is named here explicitly,
  per this repo's own convention (matching SPEC-M11's OD-44), rather than silently treated as
  already decided.
