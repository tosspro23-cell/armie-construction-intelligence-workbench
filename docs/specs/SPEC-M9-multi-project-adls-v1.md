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
(`projects/<project_id>/ifc/...`, `projects/<project_id>/pdf/...`), and where
directory-scoped access control is a real, demonstrable requirement (see §F) — exactly
the two properties ADLS Gen2's hierarchical namespace (HNS) provides over flat Blob
Storage and that D-020 found absent from the evidence-crop use case. This is the first
Azure resource in this project where ADLS Gen2 is the *correct* choice, not a
disproportionate one.

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
lifetime.** A new `ProjectResources` (ifc_repository + document_analyzers, mirroring
today's two `ServiceContainer` fields exactly) and a `ServiceContainer.get_project
(project_id: str) -> ProjectResources` method:
- When `adls_account_url` is unset: the only valid `project_id` is `"demo"`,
  resolved directly from `settings.ifc_path`/`settings.pdf_paths` exactly as today —
  this path is not new, just given a name, so every existing deployment is provably
  unaffected.
- When set: on first request for a given `project_id` in this process's lifetime,
  download that project's files from `projects/<project_id>/{ifc,pdf}/...` on ADLS
  into a local cache directory (`Settings.project_cache_dir`, new, default
  `./runtime/project_cache`) via `azure-storage-file-datalake`
  (`DataLakeServiceClient`, Managed Identity only — no API-key path, continuing every
  prior Azure resource's posture in this project), then build `IfcRepository`/
  `DocumentAnalyzer`s pointed at the cached local paths exactly as `ServiceContainer.
  __init__` does today, and cache the resulting `ProjectResources` in a dict keyed by
  `project_id` for reuse by later requests — the same in-process-cache trade-off
  already accepted for OD-22's single-replica pin, not a new architectural risk.
  An unknown `project_id` is a 404, not a silent fallback to `"demo"`.

**D. Request path threads `project_id` through, defaulting to `"demo"`.**
`ChatRequest.project_id: str | None = None` (`None` means `"demo"` — every existing
caller that has never heard of this field gets identical behaviour to before this
milestone). `/api/v1/project/*` endpoints gain a `project_id` query parameter with the
same default. `AgentService`/`graph.py`'s `_execute_pdf`/`_execute_pdf_multi_document`/
IFC execution paths receive the resolved `ProjectResources` for the request's
`project_id` instead of reading `container.document_analyzers`/`container.
ifc_repository` as deployment-wide globals — the actual refactor this milestone's
"real app-level switching" choice requires, confined to call sites that already read
those two fields (grep-verified before implementation, per this project's own
"verify, don't assume" discipline for scoping).

**E. Frontend: a project selector.** A dropdown (`apps/web/src/main.tsx`), populated
from a new `GET /api/v1/projects` endpoint (list of `{project_id, display_name}` —
empty behind the "demo"-only default when ADLS is unset, so the control can hide
itself entirely in that mode rather than presenting a meaningless single-item choice).
Selecting a project sends `project_id` on every subsequent request and — mirroring the
existing "New conversation" affordance — starts a new conversation, per Owner decision
OD-38.

**F. Azure infrastructure: `infra/bicep/adls.bicep` (new).** One Storage Account with
`isHnsEnabled: true`, `allowSharedKeyAccess: false` (Managed Identity only, matching
every other Azure resource in this project), one filesystem (container) named
`projects`. Two RBAC role assignments, both granted manually/out-of-band per D-022's
own corrected precedent (this template's own deploying identity has no more authority
to self-assign new roles here than it did for Azure AI Search):
- The API's runtime identity (`armiem3-identity`) granted **Storage Blob Data
  Reader** at the filesystem scope — it must be able to read any project a caller
  selects, so no narrower scope is possible for the identity that actually serves
  requests.
- A second, narrower role assignment **scoped by an ABAC path condition** to exactly
  one project's directory (e.g. `projects/westgate/*`), granted to a second principal
  (the operator's own identity is sufficient — no new service principal needs
  minting for a demonstration) — the concrete, falsifiable proof that ADLS Gen2's
  per-directory access control genuinely works, not merely asserted from the Bicep
  template's shape. Acceptance criteria (below) requires a real, observed `403`
  attempting to read outside that scope with the narrowly-scoped credential.

**G. Tests + a real deployment-baseline report.** Unit tests for `ServiceContainer.
get_project`'s caching/opt-in behaviour using a fake Data Lake client (D-007
discipline, no live Azure in CI) mirroring `tests/test_azure_ai_search_retrieval.py`'s
and `tests/test_evidence_blob_persistence.py`'s existing fake-client pattern; a
`TestClient(app)`-based test proving `"demo"` behaves identically with
`adls_account_url` unset versus set (same IFC/PDF content resolved either way); a
regression test that every M1–M8 single-project test still passes with no
`project_id` supplied at all. A real deployment-baseline report following this
project's established format: both projects' data actually uploaded to a live ADLS
Gen2 account, a real question answered against each project through the deployed (or
locally-run-against-real-Azure) app, and the §F ABAC-scoped-credential `403` test
performed against the real service, not simulated.

## Explicitly excluded scope

- **Per-project Azure AI Search retrieval.** `"westgate"` ships with a single PDF and
  no corpus; M7's retrieval fallback continues to apply only to `"demo"`'s existing
  index, exactly as D-022 deployed it. Building a second index or a
  `project_id`-filterable single index is real, separate scope for a future milestone,
  not folded in here to avoid this milestone growing into "M7 again, twice."
- **Per-project evidence-crop namespacing.** The existing flat `evidence` Blob
  container (D-020) is unaffected — crop filenames are already globally unique
  (`uuid4().hex`), so cross-project collision was never a real risk requiring
  reorganization.
- **Postgres schema changes for project-scoped conversation/audit isolation.** A
  `thread_id` is not validated against a single `project_id` across its turns; the
  frontend's own "start a new conversation on project switch" behaviour (§E) is the
  only enforcement this milestone provides. Documented as a known limitation, not
  silently assumed safe — revisit only if a real cross-project-context bug is ever
  observed, per this project's own "don't design for hypothetical requirements"
  discipline.
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
`ProjectResources`, `get_project`), `apps/api/app/adls.py` (new, the Data Lake client
factory + download logic, mirroring `evidence_storage.py`'s factory shape),
`apps/api/app/schemas/models.py` (`ChatRequest.project_id`), `apps/api/app/main.py`
(every `/api/v1/project/*` endpoint, new `GET /api/v1/projects`, `/api/v1/chat`),
`apps/api/app/agent/graph.py` (threading resolved `ProjectResources` instead of
reading `container.document_analyzers`/`container.ifc_repository` as globals),
`apps/api/pyproject.toml` (`azure-storage-file-datalake`), `scripts/
generate_demo_data.py` (new project-fixture generator functions),
`demo_data/projects/westgate/` (new fixture), `demo_data/projects_registry.json`
(new), `apps/web/src/main.tsx` (project selector), `.env.example`, `infra/bicep/
adls.bicep` (new), `infra/bicep/apps.bicep`/`.github/workflows/azure-deploy.yml`
(threading `adls_account_url` the same opt-in way as every prior Azure setting), new
tests, a new deployment-baseline report.

## Invariants

All prior invariants unchanged, including SPEC-M6's "every existing single-document
deployment is unaffected by default" precedent, now extended one level: every existing
single-*project* deployment (i.e., every deployment before this milestone, and every
deployment that leaves `adls_account_url` unset after it) must be provably unaffected.
New: a `project_id` never implicitly falls back to another project on a miss (an
unknown `project_id` is a 404, matching this project's existing "no guessed answer"
discipline applied to a new dimension); retrieval and IFC/PDF answers for one project
must never be able to name or cite another project's documents.

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including every pre-M9 test
  unmodified and still passing with no `project_id` argument anywhere in them —
  the direct, falsifiable proof that this milestone did not silently change
  single-project behaviour.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes.
- `infra/bicep/adls.bicep` (and any edited existing `.bicep` files) validate with
  `az bicep build`.
- If deployed: a real question answered correctly against `"westgate"` through the
  live app, naming only `"westgate"`'s own IFC/PDF content; the same for `"demo"`,
  unaffected; and a real, observed `403` from the §F ABAC-scoped credential attempting
  to read outside its granted project directory — not asserted from the Bicep
  template's shape alone.
- Every place this project's documentation has previously said ADLS Gen2 is
  unnecessary (D-018's OD-37, D-020's own framing) is updated to point at this
  milestone as the concrete case where it became justified, rather than left
  contradicting it.

## Documentation requirements

`D-023` (architecture decision record): why ADLS Gen2 is the right choice here
specifically (contrasted directly against D-020's Blob-Storage choice, not just
asserted), the `ServiceContainer`/`ProjectResources` refactor, and the ABAC
path-condition RBAC proof. `PROJECT_STATE.md` M9 milestone entry. `docs/decisions/
REVIEW_REQUIRED.md`: a new entry for the explicitly-excluded Postgres
thread/project-isolation gap (§ Explicitly excluded), matching this project's
established practice of tracking a known, deliberate limitation rather than letting
it go unrecorded.

## Git / stop conditions

One commit per subsection (A–G), spec committed alone first. Branch:
`feat/m9-multi-project-adls`. Stop and report rather than proceeding if: a
`container.document_analyzers`/`container.ifc_repository` call site is found during
implementation that this spec's Affected Surfaces list missed (i.e., `graph.py` or
`main.py` reads either field from somewhere not already named above) — that means the
refactor's actual surface is bigger than scoped, not a detail to patch around
silently; or if ADLS Gen2's ABAC path-condition role assignment turns out not to
enforce the way §F assumes on a real, live test (report the negative result plainly,
per this project's own established precedent for an unfavorable real-service result,
rather than quietly downgrading the acceptance criterion).

## Owner decisions

- **OD-38 (owner-confirmed this session).** Real application-level project switching
  (not an infrastructure-only ADLS proof) — the harder, more valuable version, chosen
  explicitly over the smaller alternative after being presented with the size
  difference.
- **OD-39 (owner-confirmed this session).** One new synthetic project in addition to
  the existing one (two total), not two new projects (three total) — enough to prove
  cross-project isolation without a second full corpus-scale fixture build.
- **OD-40 (this spec's default, flagged for owner review).** Switching a project on
  the frontend implicitly starts a new conversation, and the backend does not
  validate a thread's `project_id` consistency across turns (no Postgres schema
  change this pass) — revisit only if a real cross-project-context bug is observed
  in practice, not preemptively.
