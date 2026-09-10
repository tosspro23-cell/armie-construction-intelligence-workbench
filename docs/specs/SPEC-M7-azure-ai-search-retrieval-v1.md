# SPEC-M7 — Azure AI Search document-location retrieval

## Objective

Add an Azure AI Search-backed retrieval step that ranks configured documents by
relevance to a question, and measure it against the exact two failure modes
SPEC-M6 built evidence for — a precision failure (the same field answerable from
more than one document) and a recall failure (a realistically-phrased question's
answer exists only under vocabulary that doesn't match any table). This milestone
either demonstrates semantic retrieval clears the bar SPEC-M6 set, or documents
honestly that it does not, on the actual evidence, not a rhetorical claim either way.

## Rationale

SPEC-M6 (D-017) deliberately did not build any retrieval-by-meaning: its naive
baseline exists specifically to prove two things had to be true before Azure AI
Search was worth building at all — a genuine precision failure (proven: "Panel-A"
is a real, distinct panel independently reused across two schedules) and a genuine
recall failure (proven: `rfi_log_047.pdf` states Panel-E's capacity in prose,
using vocabulary that shares nothing with any table's "Connected Load" column).
Both now exist as real, tested fixtures. This milestone is the first to actually
build the capability they were built to justify.

**What semantic retrieval can and cannot fix — stated honestly before writing any
code, not discovered after.** Retrieval improves *recall*: finding a relevant
document despite no lexical overlap. It does not resolve *genuine* ambiguity: if
"Panel-A" is validly, equally relevant in two different documents, a retrieval
step's honest job is to surface both as relevant, the same as the naive baseline
already does — not to guess between them. This milestone's acceptance bar is
therefore asymmetric by design, not an oversight:

- On the **recall-failure** case (Panel-E): retrieval must correctly rank
  `rfi_log_047.pdf` as the most relevant document despite zero keyword overlap
  with any table — this is the actual, falsifiable "semantic beats keyword"
  claim, and the milestone fails its own purpose if this does not hold.
- On the **precision-failure** case (Panel-A): retrieval must not regress —
  it must still surface both `schedule_l2_east.pdf` and `schedule_l2_west.pdf`
  as candidates and still refuse to guess between them. Retrieval scoring one
  marginally higher than the other, by itself, is not license to silently pick
  one — see Invariants.

**The "location, never synthesis" boundary (carried forward from M1/M2P1/D-009,
restated and now load-bearing, not aspirational).** Once `rfi_log_047.pdf` is
correctly identified as the most relevant document for the Panel-E question, this
milestone still cannot read "15.75 kW" out of it — `DocumentAnalyzer.native_lookup`
only reads tables, and prose extraction is explicitly excluded (SPEC-M6's own
excluded scope, unchanged here). The honest, achievable improvement is *directedness*:
today's response is "I could not find this field with a confident match in any
configured document" (uninformative); this milestone's response can honestly become
"the most relevant document for this question is `rfi_log_047.pdf`, but I could not
automatically extract a value from it" (still `clarification_required`, still zero
fabricated values, now naming a specific document instead of a blanket miss).

**Real Azure resource already provisioned for this milestone, not hypothetical.**
`armiem3-search` (Azure AI Search, `sku: free`, `centralus` — `eastus2`, matching this
subscription's resource group, was out of capacity, the same restriction already hit
for Postgres in D-014) exists and is running, $0/hour. The existing `armie-m3-openai`
resource already has `text-embedding-3-small`, `text-embedding-3-large`, and
`text-embedding-ada-002` available for deployment (verified via `az cognitiveservices
account list-models`) — no new Azure OpenAI resource needed.

## Verified current-state assumptions

- `armiem3-search` exists (`Microsoft.Search/searchServices`, `armie-m3-rg`,
  `centralus`, `sku.name: free`, `provisioningState: Succeeded`), created during this
  session's cost/feasibility check. Free tier: no compute cost; documented platform
  limits (50MB storage, 3 indexes, 10K documents per index) are far above this
  corpus's 18 single-page documents.
- `armie-m3-openai` (the existing Azure OpenAI resource, `eastus2`) has no embedding
  deployment yet — only `gpt-5-mini` (text) is deployed. An embedding deployment is
  new infrastructure this milestone adds, on the *same* resource (no new credential
  type, extends OD-23's Managed-Identity-only posture).
- `apps/api/app/agent/graph.py`'s `_execute_pdf_multi_document` (SPEC-M6) is the only
  place a multi-document decision is made today; it calls `native_lookup` against
  every configured document unconditionally. This milestone adds a retrieval step
  *before* that loop, to rank/narrow which documents `native_lookup` actually runs
  against and to inform the honest-miss message when nothing extracts cleanly — it
  does not replace `native_lookup` as the source of any answered value.
  `apps/api/app/services.py`'s `ServiceContainer.document_analyzers` (SPEC-M6) already
  provides the per-document analyzer registry this milestone indexes.
- No document-level text is currently extracted for indexing purposes; `native_lookup`
  extracts structured table cells, not whole-document text. This milestone needs a
  separate whole-document text extraction step (PyMuPDF's own `get_text()`, already a
  transitive dependency via `fitz`/`pymupdf`, already used by `DocumentAnalyzer`) to
  build the text pushed into the search index.
- `apps/api/app/providers/factory.py`/`azure_openai_provider.py` centralize Azure
  OpenAI access (Managed Identity only, OD-23); no embedding-call path exists yet in
  `ModelProvider`'s Protocol (it currently exposes `structured`/`vision_structured`
  only) — this milestone adds one.

## Allowed scope

**A. Embedding deployment + provider seam extension.**
- Deploy `text-embedding-3-small` on the existing `armie-m3-openai` resource
  (`infra/bicep/platform.bicep` or a new `infra/bicep/search.bicep`, TBD during
  implementation) — same Managed-Identity-only access pattern as the existing text/
  vision deployments (OD-23 extends, not a new decision).
- `ModelProvider` Protocol gains an `embed(text: str) -> list[float]` method (or
  batch variant); `AzureOpenAIProvider` implements it via the new deployment.
  `OllamaProvider`/`OpenAIProvider` either implement a local/API equivalent or raise
  `NotImplementedError` — this milestone does not commit to embeddings working
  identically across all three provider backends (Azure is the only one this spec's
  acceptance criteria are evaluated against; see Explicitly excluded scope).

**B. Azure AI Search index + push-based indexing (no ADLS, no indexer pipeline).**
- One index (schema: `id`, `filename`, `content` (searchable text), `content_vector`
  (the embedding), `document_type` (schedule/spec_sheet/rfi/meeting_minutes, for
  future filtering)), created via the Azure Search SDK, Managed Identity auth
  (`disableLocalAuth` on `armiem3-search` set to disable API-key auth once the API's
  identity has the `Search Index Data Contributor`/`Search Service Contributor`
  roles it needs — continuing OD-23's zero-stored-secret posture onto this project's
  third Azure-managed resource).
- A small indexing utility (`scripts/index_document_corpus.py`, or similar) reads
  every file in `Settings.pdf_files`, extracts whole-document text via PyMuPDF,
  embeds it via the new provider method, and pushes one document per PDF into the
  index. Push-based, not an Azure AI Search indexer pulling from Blob Storage — at
  18 single-page documents, standing up ADLS/Blob Storage plus an indexer/skillset
  pipeline is disproportionate infrastructure (mirrors D-015's OD-29 "disproportionate
  infrastructure for one shared demo key" reasoning, applied here to storage). A real
  enterprise deployment would source from ADLS; this milestone explicitly does not
  build that path (see Explicitly excluded scope) and folds it into the still-open
  evidence-persistence gap's future ADLS milestone per `PROJECT_STATE.md`'s scoping
  note, not duplicated here.
- Re-running the indexing utility is idempotent (upsert by a deterministic `id`
  derived from filename), so it can be re-run after `demo_data/corpus/` changes
  without manual cleanup.

**C. Retrieval step in `_execute_pdf_multi_document`.**
- Before calling `native_lookup` against every configured document (SPEC-M6's
  current unconditional loop), query the index with hybrid search (BM25 + vector,
  Azure AI Search's built-in hybrid mode) using the question text, embedded via the
  same provider method. Retrieval scores are informational — narrowing which
  documents `native_lookup` is attempted against first, and shaping the honest-miss
  message — never a substitute for `native_lookup`'s own confidence/value.
- **Precision failure path unchanged in outcome, richer in evidence:** if
  `native_lookup` still produces more than one confident hit among the retrieved
  candidates, the response is still `clarification_required` naming every
  candidate (D-017's existing behaviour) — this milestone does not add logic to
  break that tie by retrieval score. Stated as an explicit non-goal, not a gap
  discovered later.
- **Recall failure path becomes directed, not blanket:** if `native_lookup`
  produces zero confident hits but the retrieval step ranked one or more documents
  as relevant above a threshold (this spec's OD-35), the `clarification_required`
  response names the most relevant document(s) it could not extract a clean value
  from, instead of the current "no configured document produced a confident
  deterministic match" with no direction at all. Still zero fabricated values,
  still zero synthesized answers from an unread document — only the message's
  specificity changes.

**D. Tests, evaluated against a live Azure AI Search index, not mocked.**
- Following `docs/reports/*-baseline.md` precedent (real deployment evidence, not
  asserted): a report recording the retrieval step's actual behaviour on both of
  SPEC-M6's named failure-mode questions, run against the real `armiem3-search`
  index, not `FakeModelProvider`. This is the actual acceptance evidence for this
  milestone's central claim (retrieval correctly ranks `rfi_log_047.pdf` for the
  Panel-E question) — a mocked/fake retrieval result would not prove anything a
  real Azure AI Search call could fail at (embedding drift, index schema mismatch,
  hybrid ranking behaving differently than assumed).
- Unit tests for the new `ModelProvider.embed` seam and the indexing utility's
  idempotency, using a fake/injected search client (mirroring D-007's provider-
  factory-seam pattern) — these do not require live Azure and run in CI.
- No new test requires live Azure to pass in CI; the live-index verification above
  is a manual/owner-run report, the same pattern as every prior "*-deployment-
  baseline.md" report in this project, not a CI gate.

## Explicitly excluded scope

- **ADLS Gen2 / Blob Storage / an Azure AI Search indexer-and-skillset pipeline** —
  push-based indexing directly from `demo_data/` is this milestone's entire
  ingestion path (§B). A real source-of-truth document store is the still-open
  evidence-persistence gap's concern, its own future milestone, not duplicated here.
- **Prose/table-free extraction from narrative documents** — locating
  `rfi_log_047.pdf` as relevant does not mean reading "15.75 kW" out of it; that
  remains excluded exactly as SPEC-M6 excluded it.
- **Natural-language document-name inference replacing `requested_document`** — the
  retrieval step ranks candidates for the *unset* `requested_document` case only; it
  does not attempt to resolve a question into a single named document the way a
  planner might. `QueryPlan.requested_document` stays exactly as SPEC-M6 left it.
- **Embeddings for Ollama/local-only deployments** — this milestone's acceptance
  criteria are evaluated against the Azure-backed path only; local-dev-without-Azure
  behaviour degrades to SPEC-M6's naive baseline unchanged (retrieval is skipped,
  not broken, when no embedding provider is configured — same opt-in-when-unset
  pattern as `database_url`/`api_shared_secret`).
- **Chunking documents into passages** — one embedding per whole document, not
  per-chunk. Every document in this corpus is a single page; chunking is a real
  production concern this milestone's scale does not require, and inventing chunk
  boundaries just to exercise the mechanism would not be evidence of anything.
- **Tie-breaking the precision-failure case by retrieval score** — see §C.
- **Deleting or downgrading `armiem3-search` if this milestone's evidence turns out
  negative** — see Git/stop conditions; that is an owner decision, not this spec's.

## Affected surfaces

`apps/api/app/providers/` (new `embed` method on the `ModelProvider` Protocol,
`AzureOpenAIProvider` implementation), `apps/api/app/services.py` (a search-client
factory seam, mirroring the existing provider-factory pattern), `apps/api/app/agent/
graph.py` (`_execute_pdf_multi_document`), `apps/api/app/config.py` (Azure AI Search
endpoint/index-name settings, opt-in-when-unset), `scripts/index_document_corpus.py`
(new), `infra/bicep/` (embedding deployment; `armiem3-search`'s Managed-Identity RBAC
role assignment for the API's identity), new tests, a new deployment-baseline report
under `docs/reports/`.

## Invariants

All prior invariants unchanged, including SPEC-M6's "location, never synthesis"
boundary (now enforced by two independent code paths — `native_lookup` is still the
only thing that ever produces an extracted value, regardless of what the retrieval
step ranks). New: a retrieval score, however confident, never substitutes for
`native_lookup`'s own confidence threshold, and never resolves a genuine
precision-failure collision by picking the higher-ranked candidate.

## Acceptance criteria

- The real, deployment-baseline report (§D) shows `rfi_log_047.pdf` ranked as the
  top (or tied-top) result for a Panel-E-shaped question, against the live index —
  the central falsifiable claim this milestone exists to test. If this does not
  hold on the real service, that is a valid, reportable outcome of this milestone,
  not a blocking defect to be massaged until it passes.
- The precision-failure case (Panel-A) still returns `clarification_required`
  naming both documents after this milestone, exactly as SPEC-M6 left it —
  regression-tested.
- `PYTHONPATH=apps/api python3 -m pytest -q` passes (CI-runnable subset only, per §D).
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes (only if any frontend surface needs to
  change to display the new "most relevant document" miss message — confirm during
  implementation; `citationFacts` already renders arbitrary locator keys generically,
  so this may need no frontend change at all).
- `armiem3-search`'s RBAC/Managed-Identity configuration is verified live (no API
  key embedded anywhere in code, config, or Bicep), mirroring OD-23/D-012's existing
  zero-stored-secret discipline.

## Documentation requirements

`D-018` (architecture decision record): the embedding deployment, the push-indexing
decision over ADLS/indexer pipeline, the asymmetric acceptance bar (recall must
improve, precision must not regress), and the real deployment-baseline report's
result — positive or negative, stated plainly either way. `PROJECT_STATE.md` M7
entry. `docs/decisions/REVIEW_REQUIRED.md` updated if the real-index evidence
surfaces a new, currently-untracked gap (e.g., embedding drift, index staleness
after a corpus change).

## Git / stop conditions

One commit per subsection (A–D), spec committed alone first. Branch:
`feat/m7-azure-ai-search-retrieval`. Stop and report rather than proceeding if:
the real Azure AI Search hybrid query does not rank `rfi_log_047.pdf` favorably for
the Panel-E question even after reasonable query-construction adjustments (report
the negative result plainly, per Acceptance criteria, rather than tuning the test
until it passes); `armiem3-search`'s free tier turns out to lack a capability this
milestone actually needs (e.g., semantic ranker requiring a paid tier) — check
before assuming, same discipline as the Postgres/region investigation; or the
owner decides mid-milestone that `armiem3-search` (created during the cost-check,
not a pre-planned deployment) should be deleted rather than built on — that is
explicitly the owner's call, not inferred here.

## Owner decisions

- **OD-34 (owner-confirmed 2026-09-10).** Keep and build directly on `armiem3-search`
  (the free-tier resource created while checking subscription feasibility) rather
  than deleting it and deciding deployment separately after this spec is reviewed.
- **OD-35 (needs owner confirmation).** The "relevant enough to mention by name in
  an honest miss" threshold (§C) — recommend a fixed hybrid-search relevance-score
  cutoff determined empirically from the real index (not guessed in advance), for
  the same reason `pdf_confidence_threshold` (0.75) was set from `native_lookup`'s
  own observed confidence bands (D-009), not picked arbitrarily. Confirm this
  empirical-threshold-setting approach rather than a specified value up front.
- **OD-36 (needs owner confirmation).** `text-embedding-3-small` over `-ada-002` or
  `-3-large` — recommend `3-small` as the proportional choice (cheaper than
  `3-large`, newer/better-quality than `ada-002`) for an 18-document, single-page
  corpus where embedding quality differences between these three are unlikely to
  matter at this scale. Revisit only if the real-index evidence (§D) shows the
  recall-failure case failing to rank correctly with `3-small`.
