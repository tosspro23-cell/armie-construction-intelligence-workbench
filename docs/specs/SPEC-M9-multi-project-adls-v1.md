# SPEC-M9 — Multi-project workspace via Azure Data Lake Storage Gen2

## Objective

Give this system a second, genuinely distinct construction project (its own IFC model,
its own PDF documents) that a caller can select at request time, backed by a real Azure
Data Lake Storage Gen2 account organized as one directory per project. This is the
first concrete exercise of the hierarchical-namespace capability that every prior ADLS
discussion in this project (D-018 OD-37's "needs ADLS" correction, D-020's own
Blob-vs-ADLS reasoning) deferred as "revisit only if a future milestone needs
folder-structured ... storage" — this milestone is that revisit, not a repeat of the
same theoretical argument.

## Rationale

Every milestone through M8 has one implicit project: `Settings.data_dir` (default
`./demo_data`), `Settings.ifc_file`, and `Settings.pdf_files` (SPEC-M6) are single,
deployment-wide values baked into `ServiceContainer` once at process startup
(`apps/api/app/main.py`'s `lifespan()`). M6 made the *document* dimension plural inside
one project (many PDFs, one IFC); this milestone makes the *project* dimension plural
for the first time — a genuinely different axis, not a bigger version of M6.

**Why ADLS Gen2 specifically, not another Blob container (continuing D-020's own
reasoning rather than contradicting it).** D-020 correctly kept evidence-crop storage
on plain Blob Storage because that need is opaque-filename lookup with no hierarchy.
This milestone's need is the opposite of that: real folder-per-project organization,
where a project's IFC and PDF files are naturally addressed by a path
(`projects/<project_id>/ifc/...`, `projects/<project_id>/pdf/...`), with atomic
directory-level operations and real POSIX-style directory ACLs available on that path
— exactly the hierarchical-namespace (HNS) properties Blob Storage's flat namespace
does not have, and that D-020 found unnecessary for the evidence-crop use case. This
is the first Azure resource in this project where ADLS Gen2 is the *correct* choice,
not a disproportionate one. **Independent review correction (against `b390d53`):**
an earlier draft of this section also cited "directory-scoped access control" as an
ADLS-specific property and planned to demonstrate it via an ABAC role-assignment
condition (§F) — checked against Microsoft's own documentation and found wrong:
attribute-based conditions on a role assignment are a Blob/Data-Lake-API-shared RBAC
feature, not an HNS-exclusive one, so a passing ABAC test would not actually prove
ADLS was necessary. §F now uses real Data Lake Gen2 directory ACLs (genuinely
HNS-exclusive) as the access-control proof, and treats hierarchical, atomic directory
organization — not access control — as this section's actual justification.

**Why this doesn't reopen SPEC-M7's "no ADLS/indexer pipeline" decision.** SPEC-M7
deliberately chose push-based indexing over an ADLS-backed Azure AI Search indexer
because the M6 corpus (18 documents, one project) did not need file-system semantics to
be indexed. This milestone's ADLS account stores *source* IFC/PDF files for the app to
read directly, not a corpus for Azure AI Search to crawl — retrieval is explicitly out
of scope for the new project (§ Explicitly excluded). The two decisions do not conflict
because they answer different questions.

**Why "real app-level project switching," not an infrastructure-only proof (owner
decision, this session).** A folder-structure-and-RBAC-only proof would validate ADLS
Gen2's mechanics without validating the harder, more interesting claim: that this
system's request-scoped architecture (one `ServiceContainer` per process, built once)
can be extended to resolve per-project resources without breaking any existing
single-project deployment. That extension — not the storage layer alone — is this
milestone's real engineering content.

## Verified current-state assumptions

- `apps/api/app/services.py` — `ServiceContainer.__init__` builds
  `self.ifc_repository` and `self.document_analyzers` exactly once, from
  `settings.ifc_path`/`settings.pdf_paths`, both resolved against the single
  deployment-wide `settings.data_dir`. Every other field on `ServiceContainer`
  (`conversation_store`, `audit_store`, every provider/search/blob-client factory) is
  deployment-wide and project-agnostic already — nothing about those needs to change.
- `apps/api/app/main.py` — `/api/v1/project/metadata`, `/api/v1/project/ifc`,
  `/api/v1/project/pdf`, `/api/v1/project/pdf/pages/{page}.png`,
  `/api/v1/project/viewer-elements` all read `app.state.container.{ifc_repository,
  document_analyzer(s), settings.ifc_path}` directly, with no project dimension
  anywhere in the request path. `ChatRequest` (`apps/api/app/schemas/models.py`) has
  no project field either.
- **Two additional call sites, found by independent review of `b390d53` and confirmed
  directly, not present in that draft's Affected surfaces.** (1) `main.py:313-321`'s
  `resume()` reconstructs `ChatRequest(thread_id=thread_id, question=request.answer)`
  with no project information at all — under a naive "`ChatRequest.project_id` defaults
  to `demo`" design, resuming a clarification on any other project's thread would
  silently answer against `demo` instead. (2) `apps/api/app/agent/graph.py`'s viewer
  execution path (`_execute_viewer`) builds its `Evidence` with
  `source_file=self.settings.ifc_file` — the deployment-wide global, not whichever
  project the request actually resolved — so viewer-snapshot evidence would carry the
  wrong project's filename even after `_execute_pdf`/IFC-query paths are made
  project-aware.
- `apps/web/src/IfcViewer.tsx:81` fetches `/api/v1/project/viewer-elements` directly,
  independently of `apps/web/src/main.tsx`'s own request logic and with no project
  parameter — a project selector added only to `main.tsx` would leave the 3D viewer
  itself still showing whichever project's geometry the backend defaults to.
- `apps/api/app/persistence/conversation_store.py` — `ConversationStore`'s entire
  interface today is `get(thread_id) -> dict | None` / `set(thread_id, context: dict)
  -> None`; there is no concept of a thread's project anywhere, and no atomic
  "claim this thread_id for project X unless someone already has" operation to build
  one on top of without a race.
- `IfcRepository.__init__(self, path: Path)` and
  `DocumentAnalyzer.__init__(self, pdf_path: Path, evidence_dir: Path, ...)` both take
  plain local `Path`s — neither has any Azure- or project-awareness today, and neither
  needs new constructor parameters for this milestone; what changes is *which* `Path`
  values `ServiceContainer` passes them, and when.
- `scripts/generate_demo_data.py` already has `make_ifc()`, `make_pdf()`, and
  `make_document_corpus()` (SPEC-M6) — precedent for generating a second, independent
  synthetic project's fixture the same way, not a bespoke one-off script.
- No ADLS Gen2 (or any Data Lake) account exists in `armie-m3-rg` yet (verified via
  `az storage account list` — only `armiem3sa...` legacy/log storage,
  `armiem3evidence`, both `isHnsEnabled: false`).
- The GitHub OIDC deploy identity's RBAC-delegation authority is conditioned to two
  specific role-definition GUIDs (D-012, confirmed live in D-022) — any new RBAC this
  milestone needs on a new resource must either use one of those two roles or, like
  D-022's corrected fix, be granted manually, out-of-band, the same as
  `data.bicep`/`evidence.bicep`/`armiem3-search`'s own RBAC.

## Allowed scope

**A. A second synthetic project's fixture, generated the same way M2/M6 generated
theirs.** A new, small IFC model (2 storeys, a handful of doors/windows — enough to
exercise identity/grouping queries, not a scaled-up fixture) and one PDF schedule,
under a new `demo_data/projects/westgate/` directory, generated via new functions in
`scripts/generate_demo_data.py` alongside (not replacing) `make_ifc`/`make_pdf`. No
multi-document corpus and no Azure AI Search index for this project (see Explicitly
excluded) — keeping it deliberately smaller than the existing project's fixture, since
this milestone's claim is about project *switching*, not about repeating M6's
corpus-scale work a second time.

**B. A project registry, opt-in-when-unset, existing single-project behaviour
untouched.** `Settings.adls_account_url: str | None = None`,
`Settings.adls_filesystem_name: str = "projects"` — same opt-in-when-unset pattern as
every other Azure setting in this project. When unset: exactly today's behaviour, one
implicit project (`ifc_file`/`pdf_files` as they are now), no Azure Data Lake SDK ever
imported, no new local files required. When set: a new
`demo_data/projects_registry.json` (committed, not runtime-configured — this is fixture
shape, like `pdf_files`'s own default) lists every available `project_id` with a
display name and its `ifc_file`/`pdf_files` (relative to that project's own root, not
`data_dir`) — `"demo"` (the existing project, its files re-hosted on ADLS once §F is
deployed) and `"westgate"` (new, from §A).

**C. `ServiceContainer` resolves per-project resources lazily, cached for the process
lifetime, under an explicit load protocol.** A new `ProjectResources` (ifc_repository
+ document_analyzers, mirroring today's two `ServiceContainer` fields exactly, plus
the resolved `SourceManifest` from §F) and a `ServiceContainer.get_project(project_id:
str) -> ProjectResources` method:
- When `adls_account_url` is unset: the only valid `project_id` is `"demo"`,
  resolved directly from `settings.ifc_path`/`settings.pdf_paths` exactly as today —
  this path is not new, just given a name, so every existing deployment is provably
  unaffected.
- When set: `project_id` is resolved only against `demo_data/projects_registry.json`
  (§B) — never against a caller-supplied path — and the resulting relative paths are
  rejected if they escape that project's own prefix (the same basename/containment
  discipline `GET /api/v1/evidence/{filename}` already enforces, applied to a new
  surface). An unknown `project_id` is a 404, never a silent fallback to `"demo"`.
- **Concurrency and failure contract, addressing the gap an independent review of
  `b390d53` correctly flagged** ("one replica" bounds cross-*process* races, not
  cross-*request* ones — this process already serves concurrent requests today): a
  per-`project_id` `asyncio.Lock` guards the first load, so concurrent first-requests
  for the same uncached project await one real download rather than triggering two.
  Files are downloaded (via `azure-storage-file-datalake`'s `DataLakeServiceClient`,
  Managed Identity only, run through `asyncio.to_thread` like every other blocking
  Azure SDK call already in this codebase) into a temporary directory, each verified
  against §F's manifest `content_sha256` before anything is published; the local
  cache directory is only atomically renamed into place (`Settings.
  project_cache_dir`, new, default `./runtime/project_cache`) once every file for
  that project has verified successfully. A failed or interrupted download is never
  cached, never partially published, and never silently resolved to `"demo"` — the
  request fails (502/503) and the *next* request retries the download from scratch,
  under the same lock.
- `ProjectResources` is returned to, and passed explicitly through, each call site
  that needs it (§D) — never written onto a shared mutable field on `container`
  itself. This is deliberate, not a style preference: the independent review's
  cross-project-contamination concerns (§D/§E below) are all instances of the same
  underlying risk — treating "switch project" as *mutating shared state* rather than
  *resolving a value scoped to one request* — and this is the one place in the
  design that risk gets closed structurally instead of by convention.

**D. Every conversation thread is permanently bound to one project; the request path
enforces it, not just the frontend.** `ChatRequest.project_id: str | None = None`
(`None` on a *new* thread means `"demo"`, preserving every existing caller's
behaviour). `ConversationStore` (`apps/api/app/persistence/conversation_store.py`)
gains one new method, implemented by both backends:
`bind_project(thread_id: str, project_id: str) -> str` — atomically claims
`project_id` for a thread that has never been bound, or returns the
*already-bound* project_id unchanged if one exists (so a second, differently-projected
request never silently overwrites the first). `InMemoryConversationStore` guards this
with a per-thread `threading.Lock` (calls arrive via `asyncio.to_thread`, i.e. real OS
threads, so a plain dict `setdefault` is not race-safe); `PostgresConversationStore`
adds one additive column (`project_id`, matching D-014's own "one additive SQL file"
precedent, not a new relation table) and claims it with a single `INSERT ... ON
CONFLICT (thread_id) DO NOTHING`-shaped statement, avoiding a read-then-write race at
the database level. Every pre-M9 thread is implicitly `"demo"` on first read after
this migration.
- `POST /api/v1/chat`: calls `bind_project` before resolving `ProjectResources` or
  making any tool/model call; if the returned (bound) project_id differs from the
  request's, the request is rejected with `409 project_mismatch` — before any work
  happens, not after.
- `POST /api/v1/chat/{thread_id}/resume`: today reconstructs
  `ChatRequest(thread_id=thread_id, question=request.answer)` with no project
  information at all (confirmed live in `main.py:313-321` by the independent review)
  — fixed to look up the thread's already-bound project_id and use it, never a
  request-supplied or defaulted value. `ClarificationResumeRequest` gains an optional
  `project_id` purely for a defense-in-depth consistency check (mismatch → the same
  `409 project_mismatch`), never as the source of truth.
- `apps/api/app/agent/graph.py`'s `_execute_pdf`/`_execute_pdf_multi_document`/IFC
  execution paths, and `_execute_viewer`'s `Evidence(source_file=...)` construction
  (confirmed by the independent review to currently read the deployment-wide
  `self.settings.ifc_file` global, not any per-request value), all take the resolved
  `ProjectResources`/`SourceManifest` (§C/§F) as an explicit argument instead of
  reading `container.document_analyzers`/`container.ifc_repository`/`self.settings.
  ifc_file` — every call site that reads either field, grep-verified against the
  real file before implementation, not assumed complete from this list alone.

**E. Frontend: a project selector that actually reaches every surface that renders
project-scoped content, with stale-response protection.** A dropdown (`apps/web/src/
main.tsx`), populated from a new `GET /api/v1/projects` endpoint (list of
`{project_id, display_name}` — empty behind the "demo"-only default when ADLS is
unset, so the control can hide itself entirely rather than presenting a meaningless
single-item choice). Selecting a project:
- Starts a new conversation (mirroring the existing "New conversation" affordance),
  per Owner decision OD-40.
- Clears the current selection, viewer snapshot, and evidence focus state — an
  independent-review finding that switching projects without clearing these would
  let a stale selection/evidence reference from the old project linger visually even
  though the backend has moved on.
- Is threaded into **`apps/web/src/IfcViewer.tsx`**, not only `main.tsx` — confirmed
  by the independent review to fetch `/api/v1/project/viewer-elements` independently
  (line 81), with no project parameter today. The viewer reloads its geometry from
  the newly-selected project's `project_id`.
- Tags every in-flight request with a monotonically increasing switch sequence
  number; a response that arrives after a *later* switch has already happened is
  discarded on receipt (never applied to conversation/audit/viewer state), and the
  superseded in-flight request is cancelled rather than merely ignored, closing the
  "project A's late response corrupts project B's now-active view" race the
  independent review raised.

**F. A minimal, frozen source-provenance manifest — not a full CDE, an explicit,
small addition addressing a real traceability gap.** An independent review correctly
found that a globally-unique crop filename (D-020) proves no filename collision, but
proves nothing about *which project* a piece of evidence came from or *which version*
of a source document produced it. `demo_data/projects_registry.json` (§B) is extended
with, per project, a `source_set_id` (a fixed string for this milestone — see Owner
decisions) and, per file, its `content_sha256` — computed once, checked into the
registry, and treated as immutable for this milestone: **updating a fixture's content
requires a new `source_set_id`, never an in-place overwrite of an existing one** (this
milestone's fixtures are static and public, so this is a real, checkable constraint,
not aspirational). `Evidence.locator` and every relevant `AuditEvent.payload` gain
`project_id` and `source_set_id` fields, so a trace is fully attributable end-to-end:
which project, which frozen source set, which document, produced this answer and this
evidence crop. `ServiceContainer.get_project`'s local cache (§C) is keyed by
`(project_id, source_set_id)`, not `project_id` alone, so a future source-set bump
cannot silently serve stale cached content under the same key.

**G. Azure infrastructure: `infra/bicep/adls.bicep` (new).** One Storage Account with
`isHnsEnabled: true`, `allowSharedKeyAccess: false` (Managed Identity only, matching
every other Azure resource in this project), one filesystem (container) named
`projects`.
- The API's runtime identity (`armiem3-identity`) granted **Storage Blob Data
  Reader** (an ARM role assignment) at the filesystem scope — it must be able to
  read any project a caller selects, so no narrower scope is possible for the
  identity that actually serves requests. Per D-022's confirmed precedent, the
  GitHub OIDC deploy identity's RBAC-delegation authority is conditioned to two
  specific role GUIDs and cannot self-assign a third — this assignment is granted
  manually/out-of-band, the same as every prior resource's RBAC in this project.
- A genuinely HNS-exclusive proof, corrected after independent review found the
  original plan (an ABAC role-assignment condition) does not actually demonstrate
  anything specific to ADLS Gen2 — ABAC conditions apply equally to plain Blob
  Storage. Instead: real Data Lake **directory ACLs** (a data-plane operation —
  `az storage fs access set` / the SDK's ACL API — distinct from, and not
  necessarily gated by, the ARM-level RBAC-delegation restriction above; **which
  identity is actually authorized to set a Gen2 ACL is a fact to verify live during
  implementation, not assumed to need the same manual workaround as D-022's ARM role
  assignment** — the two are different authorization mechanisms) grant a second,
  dedicated identity — freshly created for this test, holding **no other role or
  ACL** on this storage account, verified by listing its assignments before the
  test runs — read access to exactly `projects/westgate/` and nothing else. See
  Acceptance criteria for the three-part verification this identity must pass
  (positive read, negative read, and a check that no other authorization path
  explains the result), matching Microsoft's own documented caution that other
  authorization paths (a broader role, a parent-scope ACL) can make a narrow grant
  look effective when it is not actually what is being tested.

**H. Tests + a real deployment-baseline report.** Unit tests for `ServiceContainer.
get_project`'s caching, locking, and opt-in behaviour using a fake Data Lake client
(D-007 discipline, no live Azure in CI) mirroring `tests/test_azure_ai_search_
retrieval.py`'s and `tests/test_evidence_blob_persistence.py`'s existing fake-client
pattern, including a test that two concurrent first-requests for the same uncached
project trigger exactly one download; `ConversationStore.bind_project`'s
claim-once/reject-mismatch semantics, including a concurrent-first-bind test; a
`TestClient(app)`-based test proving `"demo"` behaves identically with
`adls_account_url` unset versus set; a regression test that every M1–M8
single-project test still passes with no `project_id` supplied anywhere in them. A
real deployment-baseline report following this project's established format: both
projects' data actually uploaded to a live ADLS Gen2 account, a real question
answered against each project through the deployed (or locally-run-against-real-
Azure) app, and the §G directory-ACL identity's three-part verification performed
against the real service, not simulated. See Acceptance criteria for the full,
independent-review-derived list this report must satisfy.

## Explicitly excluded scope

- **Per-project Azure AI Search retrieval.** `"westgate"` ships with a single PDF and
  no corpus; M7's retrieval fallback continues to apply only to `"demo"`'s existing
  index, exactly as D-022 deployed it. This is enforced as a real invariant, not an
  accident of `"westgate"` having too few documents to reach the multi-document
  branch (an independent review correctly flagged the original draft's phrasing as
  relying on the latter) — `_execute_pdf_multi_document` for a non-`"demo"` project
  must make zero calls through the search-client factory, asserted by call count in
  tests, the same D-007-style proof the M7 precision-failure case already uses to
  show retrieval never runs where it should not. Building a second index or a
  `project_id`-filterable single index is real, separate scope for a future
  milestone, not folded in here to avoid this milestone growing into "M7 again,
  twice."
- **Per-project evidence-crop namespacing beyond §F's manifest tagging.** The
  existing flat `evidence` Blob container (D-020) keeps its current key space —
  crop filenames are already globally unique (`uuid4().hex`); §F adds `project_id`/
  `source_set_id` to the locator/audit *metadata* around a crop, not a new storage
  layout for the crops themselves.
- **A full configuration-management/document-versioning system.** §F's manifest is
  deliberately minimal — a fixed `source_set_id` per project and a `content_sha256`
  per file, sufficient to make this milestone's two static fixtures traceable and to
  make an in-place overwrite a checkable violation rather than a silent one. It is
  not check-in/check-out, revision history, or a general versioning capability; a
  real content-management need for future non-fixture (e.g. uploaded, mutable)
  projects is separate, larger scope this milestone does not attempt.
- **True multi-tenant security isolation.** One shared `API_SHARED_SECRET` still
  gates the entire deployment (D-015); any holder can select any project. This
  milestone demonstrates data organization and Azure-side directory RBAC, not a
  customer-facing tenant boundary — see this session's own prior conclusion that real
  multi-tenancy is a different, much larger undertaking this project's current
  positioning (a public reference implementation, not a SaaS) does not call for.
  ADLS's per-directory ACL (§F) is proven at the Azure-identity layer, not extended
  into a per-end-user permission model.
- **AKS, or lifting OD-22's single-replica pin.** Unrelated to this milestone's
  actual content; already independently concluded not to be justified by anything
  this project currently needs.
- **Migrating the local-filesystem-only default away.** `adls_account_url` unset
  remains a fully supported, zero-Azure-dependency local development mode forever,
  the same as every other opt-in Azure setting in this project.

## Affected surfaces

`apps/api/app/config.py`, `apps/api/app/services.py` (`ServiceContainer`,
`ProjectResources`, `get_project`, the per-project `asyncio.Lock`/download/publish
protocol), `apps/api/app/adls.py` (new, the Data Lake client factory + verified
download logic, mirroring `evidence_storage.py`'s factory shape),
`apps/api/app/persistence/conversation_store.py`/`postgres_store.py`
(`bind_project`, the additive `project_id` column/migration),
`apps/api/app/schemas/models.py` (`ChatRequest.project_id`,
`ClarificationResumeRequest.project_id`, `Evidence.locator`'s new fields),
`apps/api/app/main.py` (every `/api/v1/project/*` endpoint, new
`GET /api/v1/projects`, `/api/v1/chat`, `/api/v1/chat/{thread_id}/resume`),
`apps/api/app/agent/graph.py` (`_execute_pdf`, `_execute_pdf_multi_document`, and
`_execute_viewer`'s `Evidence(source_file=...)` construction — all three, not only
the first two — threading resolved `ProjectResources`/`SourceManifest` instead of
reading `container.document_analyzers`/`container.ifc_repository`/`self.settings.
ifc_file` as globals), `apps/api/pyproject.toml` (`azure-storage-file-datalake`),
`scripts/generate_demo_data.py` (new project-fixture generator functions, plus a
`content_sha256` manifest-writing step), `demo_data/projects/westgate/` (new
fixture), `demo_data/projects_registry.json` (new), `apps/web/src/main.tsx` (project
selector, switch-sequence/cancellation logic), `apps/web/src/IfcViewer.tsx` (project
parameter, reload on switch), `.env.example`, `infra/bicep/adls.bicep` (new),
`infra/bicep/apps.bicep`/`.github/workflows/azure-deploy.yml` (threading
`adls_account_url` the same opt-in way as every prior Azure setting), new tests, a
new deployment-baseline report.

## Invariants

All prior invariants unchanged, including SPEC-M6's "every existing single-document
deployment is unaffected by default" precedent, now extended one level: every existing
single-*project* deployment (i.e., every deployment before this milestone, and every
deployment that leaves `adls_account_url` unset after it) must be provably unaffected.
New:
- A `project_id` never implicitly falls back to another project on a miss (an unknown
  `project_id` is a 404, matching this project's existing "no guessed answer"
  discipline applied to a new dimension).
- A conversation thread is permanently bound to the `project_id` of its first turn
  (`ConversationStore.bind_project`); any later turn — including a clarification
  resume — naming a different project is rejected with `409 project_mismatch` before
  any tool or model call, not merely discouraged by frontend convention.
- Retrieval and IFC/PDF answers, citations, and audit events for one project must
  never name, cite, or read another project's documents, IFC model, or source files
  — checked, not merely believed, via the `project_id`/`source_set_id` tagging in §F
  and the zero-search-calls assertion for non-`"demo"` projects.
- A project's local resource cache is never served, and never falls back to a
  previous version, once its content has failed integrity verification against §F's
  manifest — a corrupted or partial download is a hard failure for that request, not
  a degraded answer.

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including every pre-M9 test
  unmodified and still passing with no `project_id` argument anywhere in them —
  the direct, falsifiable proof that this milestone did not silently change
  single-project behaviour.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes.
- `infra/bicep/adls.bicep` (and any edited existing `.bicep` files) validate with
  `az bicep build`.
- **Cross-project isolation, exercised with genuinely overlapping fixture shapes, not
  just two different questions.** `"demo"` and `"westgate"` each contain at least one
  document/record/tag sharing the same name (e.g. the same board ID or storey name)
  but a different value; a question against each project must return that project's
  own correct value, never the other's.
- A thread's project binding is enforced *before* any tool or model call: a request
  that tries to continue a `"demo"`-bound thread against `"westgate"` (or vice versa)
  is rejected with `409 project_mismatch`, verified by asserting zero calls reached
  any provider/search/tool factory for that request.
- A clarification issued against `"westgate"` and resumed via
  `POST /api/v1/chat/{thread_id}/resume` stays resolved against `"westgate"` — the
  direct regression test for the `resume()` gap the independent review found live in
  `main.py:313-321`.
- Two requests against different projects, issued concurrently, each resolve their
  own project's resources and produce independently correct, non-cross-contaminated
  answers and evidence.
- A frontend project switch while a request for the previous project is still
  in-flight: that request's late response, if it arrives at all, must not alter the
  now-active project's conversation, viewer, or evidence state.
- An interrupted/corrupted download for a project never leaves a usable cache; the
  next request for that project retries cleanly and succeeds once the download
  completes without error.
- `"westgate"` (or any non-`"demo"` project) makes zero calls through the
  search-client factory for any question, asserted by call count, not inferred from
  "there is only one document."
- The §G directory-ACL identity's verification is three-part, not a single `403`:
  (1) it successfully reads a known file inside its granted `projects/westgate/`
  scope, with content matching §F's manifest; (2) it fails to read a known-existing
  file under `projects/demo/`; (3) its role/ACL assignments on this storage account
  are listed and confirmed to grant nothing beyond that scope, ruling out the result
  being explained by some other authorization path (Microsoft's own documented
  caution about ABAC/ACL interaction, applied here to a plain-ACL setup with no ABAC
  condition at all, so the same caution about "check what else could explain this"
  is followed regardless of mechanism).
- Every place this project's documentation has previously said ADLS Gen2 is
  unnecessary (D-018's OD-37, D-020's own framing) is updated to point at this
  milestone as the concrete case where it became justified, rather than left
  contradicting it — and to state precisely *which* ADLS property (hierarchical,
  atomic directory organization and real directory ACLs) justified it, not access
  control in general.

## Documentation requirements

`D-023` (architecture decision record): why ADLS Gen2 is the right choice here
specifically (contrasted directly against D-020's Blob-Storage choice, not just
asserted, and naming hierarchical-namespace/directory-ACL as the specific property —
not ABAC, per the independent-review correction above), the `ServiceContainer`/
`ProjectResources` refactor and its concurrency/failure contract, the
`ConversationStore.bind_project` thread-binding mechanism, and the directory-ACL
proof's three-part verification. `PROJECT_STATE.md` M9 milestone entry, explicitly
crediting the independent review of `b390d53` for the thread-binding, resume-path,
frontend-surface, provenance, and ABAC/ACL corrections it made before any code was
written. No new `REVIEW_REQUIRED.md` entry for thread/project isolation is needed —
unlike the earlier draft, this is now an enforced invariant (§D), not a documented
gap.

## Git / stop conditions

One commit per subsection (A–H), spec committed alone first. Branch:
`feat/m9-multi-project-adls`. Stop and report rather than proceeding if: a
`container.document_analyzers`/`container.ifc_repository`/`self.settings.ifc_file`
call site is found during implementation that this spec's Affected Surfaces list
missed — that means the refactor's actual surface is bigger than scoped, not a detail
to patch around silently; if `ConversationStore.bind_project`'s atomicity guarantee
cannot actually be made race-free against the real `PostgresConversationStore` schema
without a larger migration than "one additive column" — report and re-scope, don't
quietly weaken the guarantee to "usually correct"; or if the §G directory-ACL role
assignment turns out not to enforce the way that section assumes on a real, live
test (report the negative result plainly, per this project's own established
precedent for an unfavorable real-service result, rather than quietly downgrading
the acceptance criterion).

## Owner decisions

- **OD-38 (owner-confirmed this session).** Real application-level project switching
  (not an infrastructure-only ADLS proof) — the harder, more valuable version, chosen
  explicitly over the smaller alternative after being presented with the size
  difference.
- **OD-39 (owner-confirmed this session).** One new synthetic project in addition to
  the existing one (two total), not two new projects (three total) — enough to prove
  cross-project isolation without a second full corpus-scale fixture build.
- **OD-40 (revised after independent review of `b390d53`; this spec's default,
  flagged for owner review).** Switching a project on the frontend implicitly starts
  a new conversation (unchanged from the original draft) — but the backend *also*
  enforces one project per thread via `ConversationStore.bind_project` and a
  `409 project_mismatch` rejection (§D), correcting the original draft's reliance on
  frontend convention alone, which the independent review correctly identified as
  insufficient against a direct API caller, a stale tab, or a reused session.
- **OD-41 (this spec's default, flagged for owner review).** Each synthetic
  project's fixture is versioned by a single, fixed `source_set_id` for the life of
  this milestone — updating fixture content requires minting a new `source_set_id`
  (and therefore a new cache key, §F), never editing content behind an existing one.
  Revisit only if a future milestone needs living, editable project content, which
  is explicitly not this milestone's scope.
