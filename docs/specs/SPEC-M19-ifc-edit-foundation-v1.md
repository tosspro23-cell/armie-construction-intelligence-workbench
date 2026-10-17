# SPEC-M19: Controlled IFC Editing — Foundation (Stage 0) — v1

## 1. Objective

Lay the architectural and governance foundation for a capability this project has never had:
agent-assisted, human-approved editing of the real IFC model behind a project. This spec does
**not** implement a geometry-creating edit ("add a window") -- it implements the smallest possible
real edit (renaming one selected element) end-to-end, through a proper revision/publish/audit
pipeline, specifically to prove the foundation works before any geometry logic is built on top of
it. A follow-on spec (tentatively SPEC-M20) adds the first geometry-creating edit once this one's
own acceptance criteria are live-verified. **Stage 0 ships local-profile-only** (§11 OD-58); making
it safe on the scale-to-zero Azure deployment is a separate, explicitly deferred piece of work (§5).

## 2. Rationale

The owner relayed an independent review (from a separate model, read against this repo's own
`main` at `83f8163`) proposing that V2 gain an IFC-editing capability, starting with "add a window
to a wall." That review's technical mechanics were checked directly against the codebase (not
taken on faith) and are largely correct: `QueryPlan.operation`'s enum
(`apps/api/app/schemas/models.py:303-371`) is read/analysis-only, so an edit plan needs its own
type; V2's tool dispatch runs every tool call in parallel via
`asyncio.gather(*[asyncio.to_thread(...)])` (`apps/api/app/agent/graph.py:3137-3152`) -- a path
that has already produced one real concurrency bug (`tool_call_count` clobbered across parallel
dispatches, now fixed); `IfcRepository` memoizes its parsed model via `@cached_property`
(`apps/api/app/tools/ifc/repository.py:130-138`), so a file changed after first read is invisible
to the instance holding it; and `SourceManifest` (`apps/api/app/services.py:49-66`) is a frozen,
git-committed, hash-verified registry entry (OD-41), so in-place overwrite of the current IFC would
break the existing integrity check (`_download_verified`, `apps/api/app/services.py:320-340`).

**What that review did not address, and what this spec is actually about:** this repository makes
one specific, bolded, public claim about its own trust model --

> "the agent never writes to the IFC model or PDF drawing itself" (`README.md:157`, restated at
> `docs/architecture.md:93`)

-- as the central reason the Findings/investigation workflow (SPEC-M11/M17) is trustworthy.
**Adding any IFC-write capability reverses this claim.** Per this repository's own non-negotiable
invariants (`CLAUDE.md`: no new capability class "without an explicit, recorded owner decision")
and its "document release-claim changes" requirement, this is not a capability to add by extending
an existing tool list -- it needs its own spec, recorded Owner Decisions, and a same-PR correction
of the public claim it reverses.

**Why Stage 0 is a rename, not a window.** A geometry-creating edit mixes two different kinds of
risk: (a) the *foundation* -- safely proposing a candidate, previewing it, gating publication
behind a human, serving the right revision to every reader, and keeping an audit trail without ever
touching the committed base file; and (b) the *domain logic* -- wall-local coordinates,
`IfcOpeningElement` host relationships, and keeping Qto quantities, schema attributes
(`OverallWidth`/`OverallHeight`), and the regenerated mesh mutually consistent (the dual-source
problem `IfcRepository.door_window_dimensions_m`, D-077, already solves on the *read* side). A
rename exercises every part of (a) and none of (b), so a foundation defect is cheap to find.

**Why the human-approval gate is non-negotiable.** Findings' `approve_proposal`/`reject_proposal`
(`apps/api/app/finding_workflow.py:37`) are pure `FindingStatus` transitions -- zero side effect on
any real file. The moment a "publish" action *does* have a real side effect (every future read
serves the new revision), it must be strictly more gated than anything that exists today. No V2
tool can publish; only a dedicated, human-triggered REST call can.

## 3. Verified current-state assumptions

Checked directly against `main` at `322ce3a` (2026-10-17), not assumed:

- `QueryPlan.operation` and its sibling enums (`models.py:303`, `:371`, `:397`, `:497`) contain
  only read/analysis operations -- no write operation exists in any contract.
- `README.md:157` and `docs/architecture.md:93` are the only two statements of the "never writes"
  claim (`grep -rn "never writes to the real\|never write.*IFC"`).
- V2 dispatch is genuinely parallel (`graph.py:3137` creates the tasks, `:3152` gathers them).
- `IfcRepository.__init__` only stores `self.path` (`repository.py:122`); `model` is a
  `@cached_property` that opens the file lazily (`:130-138`) -- a new instance pointed at a
  different path is cheap and side-effect-free until first read.
- **`ServiceContainer.get_project` has two very different modes** (`services.py:185-221`):
  - *Local mode* (`adls_account_url` unset, `:199-205`): only `"demo"` resolves, and every call
    returns a **fresh `ProjectResources` wrapping the single eagerly-built `self.ifc_repository`**
    (`:131`). There is no per-project cache at all -- nothing to "invalidate"; revision-awareness
    must add a repository-per-revision mapping, not drop a cache entry.
  - *ADLS mode* (`:207-221`): resources are cached under `(project_id, source_set_id)`; the IFC
    lives under `settings.project_cache_dir` (`:224`, default `./runtime/project_cache`) --
    **container-local disk**, re-downloaded and re-verified whenever the cache is cold.
- **Since D-079 that disk is ephemeral in practice:** the API app scales to zero whenever a
  Postgres URL is configured (`infra/bicep/apps.bicep`, `minReplicas: empty(databaseUrl) ? 1 : 0`),
  and a scale-to-zero event discards the container filesystem. A revision file written there would
  vanish while a Postgres row still said "published".
- Every HTTP read endpoint resolves through `get_project` (`main.py:148` `_resolve_project`; the
  metadata, `project_ifc` (`:229`, serves `repository.path` as a file download) and
  `project_viewer_mesh` (`:336`) handlers all read `resources.ifc_repository`) -- a
  revision-aware `get_project` covers them without per-endpoint changes. `settings.ifc_path` is
  also read directly for availability flags (`services.py:371-377`), which describe the *base*
  file and are unaffected.
- The 3D viewer reloads only when `projectId` changes (`apps/web/src/IfcViewer.tsx:538`, effect
  deps `[onSelection, onStatus, onToggleHighlight, projectId]`) -- a published revision would not
  appear without a full page reload.
- Findings' `approve_proposal`/`reject_proposal` are confirmed pure state transitions
  (`finding_workflow.py:37`, `:45`).
- `FindingStore`/`ConversationStore`/`AuditStore` (`apps/api/app/persistence/`) establish this
  project's Protocol + in-memory + durable-implementation shape; `JsonlAuditStore` is the
  precedent for a *local-file* durable store used by the local profile.
- Migrations are manual, additive-only; next number is `0010` (`apps/api/migrations/`).
- `demo_data/projects/duplex/ATTRIBUTION.md` records CC BY 4.0, whose terms (quoted there) require
  attribution **and "indicate if changes were made"**; `digitalhub` is MIT. Both describe the
  *committed base file* as unmodified.
- The public Azure profile authenticates by shared secret only, not per-user identity
  (`README.md` "Known limitations").
- This subscription's OIDC federated identity trusts only `main`, so any `azure-deploy.yml` change
  can only be exercised for real after merging (PR #66's experience) -- relevant to the deferred
  Azure enablement, not to Stage 0.

## 4. Allowed scope

**A. New persistence layer: `ModelRevisionStore`** (`apps/api/app/persistence/model_revision_store.py`),
`Protocol` + two implementations -- deliberately **not** a Postgres store in Stage 0:

- `InMemoryModelRevisionStore` -- tests.
- `LocalFileModelRevisionStore` -- the Stage 0 default: a JSON manifest per project under
  `settings.model_revisions_dir` (new, default `./runtime/model_revisions`, git-ignored like the
  rest of `runtime/`), living **next to the revision IFC files it describes**. Because record and
  file share one durability domain, they can never disagree (the failure mode a durable DB row
  pointing at an ephemeral file would create). It survives API restarts on a developer machine.

```python
class ModelRevision(BaseModel):
    revision_id: str
    project_id: str
    base_source_set_id: str           # the frozen SourceManifest.source_set_id this chain started from
    parent_revision_id: str | None    # the published revision this candidate was built from; None = frozen base
    edit_plan: dict[str, Any]         # the RenameElementPlan that produced it
    file_name: str                    # `<base-stem>.rev-<revision_id>.ifc` -- never the base file's own name
    content_sha256: str
    modification_notice: str          # also written into the IFC file's own header (§4C)
    status: Literal["candidate", "published", "superseded", "rejected"]
    created_at: datetime
    published_at: datetime | None
    actor_session_id: str | None      # X-Session-Id, same documented stand-in as D-016 -- not a real identity
    trace_id: str
```

A Postgres implementation, its migration (`0010`, plus the `.github/workflows/ci.yml` apply step --
the D-024 gap), and durable file storage (Blob) are **deferred** to the spec that enables Azure
(§5, OD-58). Revision files are **immutable and never overwritten**; a publish never rewrites a file.

**B. New typed contract**, deliberately *not* a generic, extensible edit-plan type
(`apps/api/app/schemas/models.py`):

```python
class RenameElementPlan(BaseModel):
    target_global_id: str
    base_revision_id: str | None   # None = the project's frozen base
    new_name: str                  # validated: 1-255 chars, no control characters, stripped
```

One edit type, one dedicated class -- not an `edit_type` discriminator on a shared base. A second
edit type (SPEC-M20) gets its own class and spec (OD-60).

**C. New, pure IFC-edit module** (`apps/api/app/tools/ifc/editor.py`), zero model/agent/MCP awareness:

```python
def propose_rename(base_path: Path, plan: RenameElementPlan, out_dir: Path, revision_id: str) -> EditCandidate:
```

1. Open an independent copy of `base_path`; find `plan.target_global_id`; set its `Name`.
2. Write to `out_dir/<base-stem>.rev-<revision_id>.ifc` -- a name that can never be mistaken for the
   committed original.
3. Record a **modification notice** ("modified from `<original file>`; attribute `Name` of
   `<GlobalId>` changed; not the original work") in the file's own IFC header description *and* on
   the `ModelRevision` -- the CC BY 4.0 "indicate if changes were made" term for Duplex, applied
   uniformly (also cheap insurance for MIT's inclusion term).
4. **Reopen the written file fresh** (never trust the in-memory mutation) and verify the
   *fidelity invariant*: the candidate equals the base **except for exactly the one changed
   attribute** -- same set of GlobalIds, same entity counts per type, identical attributes on every
   other entity. `ifcopenshell` re-serializes the whole file on write, so "the rename worked" is not
   enough; a silent change elsewhere must fail the candidate.
5. Return the candidate path, `content_sha256`, and an explicit before/after diff
   (`old_name`, `new_name`) -- never asserts success without having reopened and compared.

**D. Two V2 tools**, offered only when `Settings.ifc_edit_enabled` is true (new, default `false`)
**and** the turn is not a finding investigation:

- `propose_ifc_edit(target_global_id, new_name)` -- resolves the project's current revision,
  calls `propose_rename`, persists a `candidate` `ModelRevision`, returns id + diff. Never changes
  what any other request is served. It writes only a brand-new uniquely-named file plus one store
  record, so running inside V2's parallel `to_thread` dispatch is safe; a candidate orphaned by a
  timed-out turn is inert by construction (never served) and harmless.
- `get_edit_result(candidate_id)` -- status/diff/validation of a previously proposed candidate.

No `publish`/`apply` tool is offered to the model, in this spec or any future one without a new
Owner Decision reversing OD-59. **Finding-investigation turns (`include_verdict_tool`) never
receive edit tools** -- an investigation's "this element" is bound to the finding's own tag (D-078),
and the README's claim that the *investigation agent* never writes stays literally true.

**E. New human-only endpoint**: `POST /api/v1/projects/{project_id}/revisions/{candidate_id}/publish`,
behind the existing app-wide `require_api_key` dependency (no new auth surface):

1. Re-verify the candidate file's `content_sha256` against disk.
2. **Compare-and-swap on the parent:** the candidate's `parent_revision_id` must equal the
   project's *current* published revision id (or `None` if none is published). Otherwise reject
   with `409 stale_candidate` -- a candidate built on an older revision can never silently overwrite
   a newer one (the lost-update bug the first draft left open).
3. Mark it `published` and the previous published revision `superseded` as one store operation,
   under a per-project lock (same pattern as `ServiceContainer._project_locks`).

**F. `ServiceContainer.get_project` becomes revision-aware without any invalidation hook.**
`get_project` first resolves the project's current published `revision_id` (a cheap store read),
then resolves resources by `(project_id, source_set_id, revision_id)`:

- *Local mode:* the base (`revision_id is None`) keeps returning the eager `self.ifc_repository`
  untouched; a revision gets its own lazily built `IfcRepository`, memoized in a new
  `_revision_repositories[(project_id, revision_id)]`.
- *ADLS mode:* `revision_id` simply joins the existing cache key (`services.py:207`).

A publish therefore needs no cache eviction at all -- the next call computes a different key and
the old entry just goes unused. This mirrors the existing docstring's own design intent ("keyed by
(project_id, source_set_id)... so a bump can never silently serve stale cached content") instead of
adding a second mechanism, and **removes the draft's "no clean invalidation hook" stop-condition**.
One `IfcRepository` per revision, never mutated (§3's `cached_property` finding is why).

**G. Minimal frontend + API surface.**
- The metadata endpoint (`main.py:179` area) reports the current `revision_id` (or `null`).
- `IfcViewer` takes `revisionId` as an effect dependency (alongside `projectId`, `:538`) so the 3D
  view reloads on publish; the viewer-mesh request carries the revision so the browser cannot serve
  a stale mesh.
- A "Propose rename" action on the selected-element panel, a diff preview, and a "Publish" button
  behind a confirmation. The active revision id (or "base") and a **"modified from original"**
  badge appear wherever `source_set_id` already does (Decision Trace, Findings).
- Browsing older revisions read-only is a stretch goal, not required for §8.

## 5. Explicitly excluded scope

- No geometry-creating or geometry-modifying edit (add/move/resize a window, door, wall, opening)
  -- SPEC-M20, contingent on this foundation being live-verified first. The reviewed proposal's own
  first capability ("add a window to a wall") is deferred, not folded in.
- No Qto/engineering-quantity recomputation -- a rename touches no quantity; SPEC-M20 must solve
  it, with `IfcRepository.door_window_dimensions_m` (D-077) as the dual-source pattern to keep
  consistent on the write side.
- **No Azure enablement.** A Postgres-backed store, durable (Blob) revision-file storage, and
  re-verifying D-079's `minReplicas` coupling are a separate follow-up spec (OD-58); until then
  `ifc_edit_enabled` must remain `false` on any deployment with scale-to-zero.
- No MCP surface. Wrapping a capability that does not safely exist yet is premature.
- No autonomous publish, by the model or any automated process (Invariant 1).
- No cross-project or cross-thread edit target -- an edit always targets the project already bound
  to the calling thread (D-016/SPEC-M9).
- Committed base IFC/PDF files under `demo_data/` and their `content_sha256` in
  `projects_registry.json` are never written to, and **no revision file is ever committed to git**
  -- revisions are additive, uncommitted, separate storage, so "unmodified" stays literally true of
  the committed base file.
- No revision-history browsing/diff UI beyond direct-by-id resolution.
- No change to V1, to reconciliation's join logic, to OD-15's door/window-only scope, or to any
  existing read-path contract or test.

## 6. Affected surfaces

| File | Change |
|---|---|
| `apps/api/app/schemas/models.py` | New `RenameElementPlan`; new `ModelRevision`. |
| `apps/api/app/persistence/model_revision_store.py` | New `ModelRevisionStore` Protocol + `InMemory…` + `LocalFile…` implementations. |
| `apps/api/app/tools/ifc/editor.py` | New pure `propose_rename` + `EditCandidate`; fidelity check; header modification notice. |
| `apps/api/app/config.py` | `ifc_edit_enabled` (default `false`), `model_revisions_dir`. |
| `apps/api/app/agent/tools.py` | `PROPOSE_IFC_EDIT_TOOL` / `GET_EDIT_RESULT_TOOL`, offered conditionally (§4D). |
| `apps/api/app/agent/graph.py` | `invoke_v2` (conditional offer; never on investigation turns); `_v2_dispatch_tool` local dispatch for the two tools. Existing parallel-dispatch path untouched. |
| `apps/api/app/services.py` | Revision-aware `get_project`; `_revision_repositories`; revision joins the ADLS cache key. |
| `apps/api/app/main.py` | `POST …/revisions/{id}/publish`; metadata reports `revision_id`; viewer-mesh/`project_ifc` accept the revision implicitly via `get_project`. |
| `apps/web/src/IfcViewer.tsx`, `apps/web/src/main.tsx` | `revisionId` dep + reload; "Propose rename"/diff/"Publish"; revision + "modified" badge. |
| `README.md`, `docs/architecture.md` | Correct the "never writes" claim (§9) in the same PR as the code. |
| `demo_data/projects/{duplex,digitalhub}/ATTRIBUTION.md` | One added sentence each: derived revisions exist outside git, carry a modification notice, and are not the committed original. |
| `docs/decisions/README.md`, `PROJECT_STATE.md`, `docs/decisions/REVIEW_REQUIRED.md` | `D-080`; M19 entry; tracked Azure-enablement gap. |

## 7. Invariants

1. No V2 tool, in this spec or any future one, can publish a candidate as a project's current
   revision -- only the human-triggered endpoint (§4E). Reversing this needs OD-59's own reversal.
2. The committed base IFC/PDF files and their recorded `content_sha256` are never modified; no
   revision file is ever committed -- confirmed by a live before/after hash check (§8).
3. A candidate is publishable only if it passed `propose_rename`'s reopen-and-**fidelity** check
   (identical to the base except the one target attribute) and its recorded hash still matches disk.
4. A candidate can only be published on top of the revision it was built from (compare-and-swap,
   §4E.2); a stale candidate is rejected, never merged.
5. Revision files are immutable and uniquely named; a revision never reuses or overwrites any file,
   and never carries the committed original's file name.
6. Every proposed/published edit is attributed to the request's `X-Session-Id` and recorded durably
   in `ModelRevisionStore` -- not an unaudited side channel (D-016's precedent for what that id does
   and does not prove).
7. Finding-investigation turns never receive edit tools; the investigation agent still never
   writes (`README.md:157`'s claim remains true of it).
8. `ifc_edit_enabled` defaults to `false`, and must stay `false` on any deployment that can scale to
   zero until the deferred Azure spec lands.
9. OD-15's door/window reconciliation scope, every V1 path, and every existing read contract
   (`QueryPlan`, `Evidence`, `Citation`, `VerificationStatus`) are unaffected.

## 8. Acceptance criteria

- End-to-end, local profile, demo project: select an element, `propose_ifc_edit` with a new name,
  receive a diff computed from an **actually reopened** candidate file, publish it, then ask a V2
  question that reads the element back and get the **new** name -- with no restart and no manual
  cache clear (§4F), and **with the 3D viewer showing the new name without a page reload** (§4G).
- **Restart persistence:** after restarting the API, the published revision is still what is served
  (`LocalFileModelRevisionStore`), and a candidate's record and file never disagree.
- **Stale candidate:** two candidates built from the same parent -- publishing the first succeeds,
  publishing the second returns `409 stale_candidate`; a concurrent double-publish yields exactly
  one winner.
- **Fidelity:** a deliberately corrupted candidate (a second attribute changed on another entity)
  is rejected by §4C.4 and never publishable -- demonstrated with a real altered file, not a mock.
- **Real buildings:** `propose_rename` run against both the Duplex and RWTH DigitalHub IFCs passes
  the fidelity check, with **measured** duration and output size recorded in D-080 (ifcopenshell
  re-serialization cost on a real file is unknown today; it is measured, not assumed).
- Modification notice present in the candidate file's own header and on the record; the file served
  by `GET /api/v1/project/ifc` for a revision has the distinct `…rev-<id>.ifc` name.
- The committed `demo_data/` IFC has an unchanged `content_sha256` before and after the whole flow.
- A V2 finding-investigation turn is confirmed not to be offered the edit tools.
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests`, and
  `(cd apps/web && npm run build)` all clean, with no flags.
- The corrected public claim (§9) lands in the same PR as the code.

## 9. Documentation requirements

- `docs/decisions/README.md`: `D-080` -- the design, the trust-claim reversal and why, the
  measured real-building results, and the Stage 0 verification account.
- `README.md` / `docs/architecture.md`: rewrite "the agent never writes to the IFC model or PDF
  drawing itself" to the new, precise truth -- a human-gated, revision-based edit path exists for a
  narrow, listed set of edit types, locally; no agent turn can publish; finding investigations
  still never write. Same PR as the code (release-claim change per `CLAUDE.md`).
- `demo_data/projects/{duplex,digitalhub}/ATTRIBUTION.md`: the one-sentence derived-revision note.
- `docs/decisions/REVIEW_REQUIRED.md`: a tracked entry -- "IFC editing is local-only; Azure needs
  Postgres store + Blob file storage + a re-check of D-079's scale-to-zero coupling."
- `PROJECT_STATE.md`: new M19 milestone entry.

## 10. Git / stop conditions

- Branch: `feat/m19-ifc-edit-foundation`. One commit per lettered subsection of §4, plus a separate
  docs commit for §9. This spec itself is committed alone, first, per `CLAUDE.md`.
- **Stop and report, do not improvise, if:**
  - the "never writes" claim turns out to have copies beyond `README.md:157` / `architecture.md:93`
    -- a partial correction is worse than none;
  - the revision-keyed `get_project` cannot be made to cover some read path (any code reading
    `settings.ifc_path` or `container.ifc_repository` for *content* rather than availability);
  - `ifcopenshell` cannot round-trip a real building such that the fidelity check (§4C.4) can pass
    at all -- that is a finding about the whole approach, to be reported, not relaxed;
  - any part of the scope would require weakening Invariant 1, 2, 4 or 7.

## 11. Owner decisions

All five decided on 2026-10-17 after an independent review pass against `main` at `322ce3a` (which also produced OD-61 and the §3/§4 corrections). Implementation still waits on this spec being committed and merged (spec-first, `CLAUDE.md`).

- **OD-57** -- authorize pursuing agent-assisted IFC editing at all, knowingly reversing the
  public "never writes" claim (§2) and documenting that from Stage 0 onward. *Status: DECIDED 2026-10-17 (owner) -- authorized for Stage 0 (rename) only; the public claim is corrected in the same PR as the code (§9).*
- **OD-58** -- Stage 0 is local-profile-only; the public shared-secret Azure demo does not get it
  until a per-user/per-project authorization story *and* durable revision storage exist (§3: the
  scale-to-zero container disk is ephemeral; the public demo also sits beside two real,
  openly-licensed buildings). *Status: DECIDED 2026-10-17 (owner) -- Stage 0 is local-only; Azure enablement is a separate, later spec (§5).*
- **OD-59** -- the human-approval publish gate (§4E, Invariant 1) is durable, not provisional: any
  future spec proposing a model-triggered publish must name and explicitly reverse this decision.
  *Status: DECIDED 2026-10-17 (owner) -- durable.*
- **OD-60** -- `RenameElementPlan` stays its own single-purpose type (§4B); the second edit type
  gets its own type and its own spec, never a new enum value. *Status: DECIDED 2026-10-17 (owner) -- single-purpose type, no `edit_type` enum.*
- **OD-61** *(new, raised by this review)* -- Stage 0's revision store is the local-file store
  (§4A), with Postgres + Blob deferred to the Azure spec, rather than building the Postgres store
  now. *Status: DECIDED 2026-10-17 (owner) -- local-file store (`LocalFileModelRevisionStore`); Postgres + Blob deferred to the Azure spec.*
