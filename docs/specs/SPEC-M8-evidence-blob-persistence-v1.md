# SPEC-M8 — Evidence crop persistence via Azure Blob Storage

## Objective

Make cited PDF evidence crops (`Evidence.locator["evidence_crop"]`) survive an Azure
Container App revision replacement, closing the one piece D-014 (SPEC-M4) explicitly
left open when it made conversation context and audit history durable via Postgres.

## Rationale

`DocumentAnalyzer.crop_evidence` (`apps/api/app/tools/document/analyzer.py`) writes
cited-region PNGs to `Settings.evidence_dir`, a local Container App filesystem path.
`Evidence.locator["evidence_crop"]` (the filename) is what actually gets persisted
durably today, via SPEC-M4's Postgres-backed `AuditStore`/`ConversationStore` — but the
PNG file itself is not: a Container App revision replacement wipes local disk, so a
user revisiting an old conversation, or an auditor following an old trace's citation,
gets a broken image for evidence the system's own database still describes in full
detail. `docs/decisions/REVIEW_REQUIRED.md`'s "audit/evidence is not persisted across
Container App revisions" finding has flagged this since D-012; SPEC-M4 explicitly
scoped it out (OD-24) as needing "its own, larger milestone" — this is that milestone.

**Narrower than "persist everything `evidence_dir` ever writes," verified before
scoping, not assumed:** grep-verified that `DocumentAnalyzer.render_page`'s output
(also written under `evidence_dir`) is never stored by filename anywhere — every call
site either serves it once immediately (`/api/v1/project/pdf/pages/{page}.png`) or
feeds it straight into a vision-model call as base64 and discards the path. Only
`crop_evidence`'s output filename is ever embedded into a `locator` that gets
persisted. This milestone accordingly touches only `crop_evidence` and the
`/api/v1/evidence/{filename}` serving endpoint — `render_page` and its two call sites
are unaffected, on purpose, not by oversight.

**Blob Storage, not literally "ADLS Gen2"** despite how this gap has been informally
named in prior documents (`PROJECT_STATE.md`'s Phase 3 scoping note, D-018's addendum).
Verified before scoping: this project's actual need is retrieving a PNG by an opaque
filename key, with no folder hierarchy, path-based query, or analytics workload ever
performed against evidence crops — ADLS Gen2's defining feature (a hierarchical
namespace) has no use here. Plain Azure Blob Storage (Standard tier, a flat container)
is the proportional choice, mirroring OD-29's "disproportionate infrastructure for
[a narrow, small need]" reasoning already established for this project's Key Vault
and ADLS/indexer-pipeline decisions (D-015, D-018).

## Verified current-state assumptions

- `apps/api/app/tools/document/analyzer.py:82-95` — `crop_evidence` writes a PNG via
  `pixmap.save(output)` to `self.evidence_dir / f"pdf-crop-{page_number}-{uuid4().hex}.png"`
  and returns the local `Path`; every call site (`analyzer.py:288,372,421`, `graph.py:1296`)
  uses only `crop.name` afterward — the actual file content is never re-read
  in-process after being written.
- `apps/api/app/main.py:154-163` — `GET /api/v1/evidence/{filename}` resolves
  `settings.evidence_dir / filename` and 404s if the local file is missing; basename
  containment (`Path(filename).name != filename` rejected) is an existing invariant,
  unaffected by this milestone.
- `apps/api/app/services.py` — `ServiceContainer` constructs one `DocumentAnalyzer`
  per configured PDF file (SPEC-M6), each given the same `settings.evidence_dir` —
  evidence crops from every configured document currently land in one shared local
  directory, distinguished only by their randomized filename.
- No Azure Storage Account exists yet in `armie-m3-rg` (verified via `az storage
  account list`) — this is genuinely new infrastructure, not a reconfiguration of
  something already provisioned (unlike SPEC-M7's Azure AI Search work, which reused
  the cost-feasibility-check resource).

## Allowed scope

**A. Config + a blob-container-client factory, opt-in-when-unset.**
- `Settings.evidence_storage_account_url: str | None = None`,
  `Settings.evidence_storage_container_name: str = "evidence"` — same
  opt-in-when-unset pattern as `database_url`/`azure_search_endpoint`: unset means
  local-filesystem-only evidence storage, today's exact behaviour, no Azure resource
  required for local development.
- `apps/api/app/evidence_storage.py` (new): `get_blob_container_client(settings)`,
  mirroring `app/retrieval.py`'s `get_search_client` exactly — returns `None` unless
  `evidence_storage_account_url` is set; Managed Identity only
  (`DefaultAzureCredential`), no API-key/connection-string code path at all,
  continuing OD-23's zero-stored-secret posture onto this project's fourth
  Azure-managed resource (Postgres, Azure AI Search, now Blob Storage).

**B. `DocumentAnalyzer.crop_evidence`: additionally upload to blob storage when
configured, local write unchanged.**
- `DocumentAnalyzer` gains an optional `blob_container_client` constructor
  parameter (default `None`), injected once by `ServiceContainer` and shared across
  every configured document's analyzer instance (one container, many documents —
  filenames are already globally unique via `uuid4()`, no per-document prefixing
  needed).
- `crop_evidence` keeps writing the local file exactly as today (harmless, cheap,
  and avoids changing its return type or any call site) and, when a container client
  is present, additionally uploads the same PNG bytes under the same filename. A
  transient upload failure is logged (this project's existing audit-on-failure
  pattern) but does not fail the request — the evidence is already correctly
  described in the citation/locator either way; losing durability for one crop is a
  strictly lesser failure than losing an already-computed, already-verified answer
  (the same proportionality SPEC-M4's `context_persist_error` handling already
  established for conversation-context writes).

**C. `GET /api/v1/evidence/{filename}`: blob storage first when configured, local
fallback.**
- When a container client is configured: try blob storage first (the durable source
  of truth once enabled); fall back to the local file if blob storage doesn't have
  it (covers evidence generated in the same container instance's lifetime before an
  upload completes, and evidence generated before this milestone was ever enabled);
  404 only if neither has it.
- When unconfigured: unchanged, exactly today's local-only lookup.

**D. Azure infrastructure.**
- One Storage Account (Standard LRS, a proportional SKU for PNG crops a few KB
  each), one Blob container (`evidence`, private access). RBAC: the API's runtime
  identity (`armiem3-identity`) granted `Storage Blob Data Contributor` (read +
  write — unlike D-018's Search setup, there is no separate "operator vs runtime"
  split here, since the running API is the only writer, not a batch indexing script).
- `infra/bicep/` gains the Storage Account + container + role assignment,
  matching the existing Bicep-per-resource pattern (`data.bicep` for Postgres).
  `azure-deploy.yml` threads `evidence_storage_account_url` through alongside the
  other opt-in Azure settings, deployed but not force-enabled (the app.state
  behaviour on an omitted setting is unchanged local-disk-only, matching
  `DATABASE_URL`'s own "leaving it unset silently reverts to the pre-milestone
  state, and that must not happen by accident" caution from D-014 — see
  Acceptance criteria).

**E. Tests.**
- Unit tests for `get_blob_container_client`'s opt-in behaviour (returns `None`
  unless configured) and `crop_evidence`'s dual-write, using a fake container
  client injected the same way `tests/test_azure_ai_search_retrieval.py` injects a
  fake search client (D-007 discipline) — no live Azure, no network, in CI.
- A `TestClient(app)`-based end-to-end test: with a fake container client
  configured, generate a citation with an evidence crop, delete the local file to
  simulate a revision replacement, then confirm `GET /api/v1/evidence/{filename}`
  still serves it correctly from the fake blob store.
- A real deployment-baseline report (this project's established pattern) verifying
  against the actual live Storage Account: generate a real answer with a citation,
  note the evidence filename, delete the local file directly (simulating what a
  revision replacement does), and confirm the API still serves it — the actual
  claim this milestone exists to prove, not assumed from unit tests alone.

## Explicitly excluded scope

- **`render_page`'s output** — never persisted by filename anywhere; out of scope,
  verified above, not overlooked.
- **ADLS Gen2's hierarchical namespace, any indexer/skillset pipeline** — this
  milestone is a flat Blob container, nothing more; SPEC-M7's own "no ADLS" decision
  for document *source* storage is a separate, still-open question this milestone
  does not reopen.
- **Migrating existing local evidence files into blob storage retroactively** — a
  fresh deployment starts persisting new evidence only; old local-only evidence from
  before this milestone was enabled is not backfilled.
- **Evidence storage lifecycle/retention policies, encryption-at-rest configuration
  beyond Azure Storage's own default** — none of this project's other Azure
  resources have bespoke lifecycle rules either; not introduced here as a special
  case for one resource.
- **Multi-region or geo-redundant storage** — Standard LRS only, matching this
  project's single-region deployment posture everywhere else.

## Affected surfaces

`apps/api/app/config.py`, `apps/api/app/evidence_storage.py` (new),
`apps/api/app/tools/document/analyzer.py` (`crop_evidence`, constructor),
`apps/api/app/services.py` (`ServiceContainer`, blob-client factory injection),
`apps/api/app/main.py` (`evidence_file`), `.env.example`, `infra/bicep/` (new
Storage Account + container + role assignment), `.github/workflows/azure-deploy.yml`,
new tests, a new deployment-baseline report.

## Invariants

All prior invariants unchanged. New: an evidence crop's citation locator must remain
resolvable via `GET /api/v1/evidence/{filename}` after the local file that produced
it no longer exists, whenever `evidence_storage_account_url` is configured — this is
the actual, falsifiable claim SPEC-M4's OD-24 deferred and this milestone exists to
prove, not merely implement.

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including new tests, with every
  existing evidence-related test unmodified and still passing.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes (no frontend change expected — the
  evidence-crop `<img>`/`AuthedImage` fetch path already treats the endpoint as an
  opaque URL and does not need to know where the bytes actually come from).
- `infra/bicep/*.bicep` files validate with `az bicep build`.
- If deployed: the real deployment-baseline report's core claim (local file deleted,
  endpoint still serves the crop from blob storage) is demonstrated against the live
  Storage Account, not asserted from local tests alone.
- Mirroring D-014's own corrected mistake: this milestone's own documentation must
  not claim evidence durability is the default — an unmodified deployment (blob
  storage left unset) stays exactly as ephemeral as it is today, and every place
  this gap is currently described as open must be updated to say so precisely
  ("resolved once configured," not "resolved").

## Documentation requirements

`D-020` (architecture decision record): the Blob-Storage-not-ADLS-Gen2 scope
correction (OD unchanged from this spec's own framing), the dual-write design, and
the RBAC setup. `PROJECT_STATE.md` M8 milestone entry. `docs/decisions/
REVIEW_REQUIRED.md`'s "audit/evidence is not persisted across Container App
revisions" finding updated to reflect what this milestone closes (the evidence-crop
half) versus what was already closed by D-014 (conversation/audit half).

## Git / stop conditions

One commit per subsection (A–E), spec committed alone first. Branch:
`feat/m8-evidence-blob-persistence`. Stop and report rather than proceeding if: a
call site of `crop_evidence` or `render_page` is found during implementation that
this spec's Verified current-state assumptions missed (i.e., `render_page`'s output
turns out to be stored by filename somewhere after all) -- that would mean the
narrower scope this spec chose is wrong, not a detail to quietly patch around.

## Owner decisions

- **OD-37 (this spec's default, flagged for owner review).** Plain Azure Blob
  Storage (Standard LRS, flat container), not ADLS Gen2 — this project's actual
  need (opaque-filename PNG lookup, no hierarchy, no analytics) does not benefit
  from ADLS Gen2's hierarchical namespace, and the informal "needs ADLS" framing in
  prior documents was never a deliberate decision to require it specifically.
  Revisit only if a future milestone needs folder-structured or analytics-queryable
  evidence storage, neither of which is a current requirement.
