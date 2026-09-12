# M9 — Multi-Project ADLS Gen2 Baseline (live, real evidence)

Owner-authorized run against the same Azure subscription as the M3-M8 baselines,
2026-09-12. Records real, measured evidence for SPEC-M9's two central claims --
(1) Data Lake Gen2 directory ACLs, not RBAC/ABAC, are what genuinely scope one
project's data away from another; (2) the deployed application, not just Bicep
accepting a parameter, actually switches projects end-to-end -- per this project's
own verification discipline that a fix is not "done" because tests pass.

## What was deployed

- `armiem3proj<suffix>` (Storage Account, `isHnsEnabled: true`, `Standard_LRS`,
  `allowSharedKeyAccess: false` -- no API-key/connection-string path exists,
  matching D-014/D-018/D-020's precedent for this project's other Azure data
  resources), one filesystem (`projects`).
- Two project trees under it: `demo/{ifc,pdf}/...` (mirroring the existing
  local `demo_data/` layout) and `westgate/{ifc,pdf}/...` (a new, minimal
  second fixture: one storey, one door, one window, its own schedule PDF with
  a deliberately-shared board name, `Panel-A`, at a distinct value).
- RBAC: the API's runtime identity (`armiem3-identity`) granted `Storage Blob
  Data Reader` at the storage-account scope (read-only; the running API never
  writes to this account) -- `infra/bicep/adls.bicep`.
- A directory ACL on `westgate/` and its parent traversal path, granting a
  single test identity `r-x`/`--x` access, set out-of-band via `az storage fs
  access set` (this milestone's proof mechanism, not RBAC).

## Central claim 1: does the directory ACL, not RBAC, actually gate access?

RBAC and ACL are OR'd in ADLS Gen2's authorization model (either grants access),
so a test that only grants an ACL and then succeeds could still be passing
because of some other RBAC grant nobody accounted for. The test below is
structured specifically to rule that out.

**Setup.** Created a fresh App Registration/Service Principal
(`az ad sp create-for-rbac --skip-assignment`) -- deliberately never the
operator's own `az` CLI session, and never reusing `armiem3-identity` --
granted **zero** RBAC roles on the storage account, and set exactly one
directory ACL entry (`user:<oid>:r-x` on `westgate/` and its parent path,
`user:<oid>:r--` on the leaf file, `--x` traversal on the root) via `az storage
fs access set --acl "user::rwx,group::r-x,other::---,user:<oid>:r-x"`
(full-ACL-replacement syntax -- owner/group/other explicitly preserved, not
just the new entry appended). Acquired an OAuth2 client-credentials token for
this identity via a direct REST call to `login.microsoftonline.com`, then made
direct REST calls to the storage account's `.dfs.core.windows.net` endpoint --
never through the Azure SDK's own credential-caching layer, so there is no
ambiguity about which identity's token is on the wire.

**Positive read.** A `GET` against `westgate/pdf/westgate_schedule.pdf` with
that token returned `200`, with response-body bytes hash-identical
(`sha256`) to the committed fixture file -- the ACL genuinely serves real
content, not just a permissions check that happens to succeed.

**Negative read.** The identical request pattern against `demo/pdf/
armie_demo_schedule.pdf` -- a path this identity was never granted any ACL
entry on -- returned `403 AuthorizationPermissionMismatch`. Confirms the ACL
is scoped to exactly the granted path, not the whole filesystem or account.

**Audit of no other authorization path.** `az role assignment list` against
the storage account scope, filtered to this test identity's principal ID,
returned zero rows -- ruling out the possibility that some inherited or
default RBAC grant (not this milestone's ACL) explains the positive read
above. The ACL itself, read back via `az storage fs access show`, matched
exactly what was set: no broader entry, no unexpected inherited ACL from a
parent directory beyond the deliberate traversal-only `--x` grants.

Together, these three results are the actual, falsifiable version of "ADLS
Gen2's directory ACLs are what this project's per-project access control
needs" -- not an assumption from HNS being enabled, and not conflated with
the RBAC role-assignment conditions (ABAC) that an earlier spec draft had
proposed as the proof mechanism, which would have worked identically on
plain Blob Storage and proven nothing ADLS-specific.

## Central claim 2: does project switching actually work on the deployed app?

### Backend: `azure-deploy.yml`'s own smoke test, run to completion

Three real deployment attempts (`workflow_dispatch` runs) were needed before
this smoke test ran to completion at all -- each of the first three surfaced a
distinct, real defect (missing `thread_projects` migration/grant on the real
Postgres instance, demo's ADLS registry entry not matching its real deployed
`PDF_FILES` corpus, and `document_analyzers` keyed by nested manifest path
instead of basename once ADLS mode resolved "demo" through the new code path)
plus a fourth defect in the smoke test's own stale expectation once the third
was fixed. All are recorded in D-023 (`docs/decisions/README.md`) with root
cause and fix for each. The run that finally passed end-to-end:

```
GET /api/v1/projects            -> ["demo", "westgate"]
POST /api/v1/chat  {project_id: "demo",     question: "...DB-L1-A?"}  -> answered, 18.50
POST /api/v1/chat  {project_id: "westgate", question: "...Panel-A?"}  -> answered, 51.20
POST /api/v1/chat  {thread_id: <demo's thread>, project_id: "westgate", question: "test"}
                                 -> HTTP 409
```

`Panel-A` is deliberately the same board name in both projects' own corpora,
at a different value -- the direct proof that a question against one project
can never resolve using the other's data, rather than merely each project
answering correctly in isolation.

### Frontend: a real browser walkthrough against the redeployed app

Backend smoke tests passing does not, by itself, prove the feature is usable
-- and it did not: the first post-fix walkthrough found the project selector
never rendered at all (defect 5 in D-023, a frontend-only race between the
access-key gate and a mount-time effect with an empty dependency array).
After that fix and a redeploy, re-verified directly in a real browser
(Authorization: Bearer, the shared access key -- not a service-to-service
call):

- Logged into `armiem3-web` fresh (a new browser tab, empty `sessionStorage`,
  matching exactly the state every real user starts a session in) with the
  shared access key -- the "Project" selector now renders, listing "ARMIE
  Demo Project" and "Westgate Distribution Center."
- Switched to Westgate: the IFC viewer tore down and rebuilt with Westgate's
  own geometry (a single door-frame element, matching its minimal fixture);
  the conversation panel reset (SPEC-M9's OD-40: a project switch implicitly
  starts a new conversation).
- Asked "What is the connected load for Panel-A?" against Westgate: answered
  `51.20`, with a cited evidence locator ("pdf: Native table cell: Panel-A ·
  Connected Load (kW) = 51.20"), verification `passed`.
- Switched back to "ARMIE Demo Project": the viewer and conversation reset
  again. Asked "What is the connected load for DB-L1-A?" (the smoke test's
  own disambiguated demo question, since asking about "Panel-A" here
  correctly triggers SPEC-M6's own multi-document collision instead):
  answered `18.50`, with a cited evidence locator ("pdf: Native table cell:
  DB-L1-A · Connected Load (kW) = 18.50"), verification `passed`.

This is the actual, evidenced version of "real application-level project
switching" (OD-38) the owner chose over an infrastructure-only proof -- not
inferred from Bicep validating or from the backend smoke test alone.

## Not yet done, stated plainly

Directory-ACL scoping was verified for exactly the one path (`westgate/`)
this baseline granted a test identity access to -- it was not repeated for
every project/file combination, nor exercised under concurrent writers (this
project has no ADLS write path from the running API at all; every write here
was an out-of-band operator upload). Cross-*process* concurrent-first-access
behavior (two separate `armiem3-api` replicas racing to resolve the same
uncached project) remains untested against the real service, same as OD-22's
existing single-replica pin for every other in-process state this project
already carries -- the CI-safe suite's concurrency test (`tests/
test_multi_project_adls.py`) covers the cross-*request*, same-process case
only, which is the race this milestone's own spec review actually flagged as
previously unhandled.
