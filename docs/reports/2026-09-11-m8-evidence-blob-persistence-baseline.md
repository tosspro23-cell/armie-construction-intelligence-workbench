# M8 — Evidence Crop Blob Persistence Baseline (live, real evidence)

Owner-authorized run against the same Azure subscription as the M3/M4/M5/M6/M7
baselines, 2026-09-11. Records real, measured evidence for SPEC-M8's central claim,
per this project's own verification discipline: a fix is not "done" because tests
pass, and a persistence claim specifically needs proof against the real service, not
an assertion from local fakes alone.

## What was deployed

- `armiem3evidence` (Storage Account, `Standard_LRS`, `StorageV2`, `eastus2` --
  no region-capacity restriction hit this time, unlike Postgres/Azure AI Search).
- `allowSharedKeyAccess: false` set immediately after creation -- no API-key/
  connection-string code path exists in `apps/api/app/evidence_storage.py` at all;
  `DefaultAzureCredential` is the only credential mechanism, matching D-014's
  `passwordAuth: 'Disabled'` and D-018's `disableLocalAuth: true` precedent for this
  project's other two Azure data resources.
- One container, `evidence`, private access (`publicAccess: 'None'`).
- RBAC: the API's runtime identity (`armiem3-identity`) granted `Storage Blob Data
  Contributor` directly -- no separate operator/runtime split this time (unlike
  D-018's Search setup), since the running API is the only writer, not a batch
  indexing script.

## Central claim: does a citation's evidence crop survive its local file being gone?

Ran a real, deterministic (`FakeModelProvider`, zero live-model cost) chat request
against the real `armiem3evidence` account:

```
question: "What is the connected load for Panel-A?"
disposition: answered
citation evidence_crop: pdf-crop-1-ae2e207c2c52413cb859c7be1651960a.png
```

Confirmed the crop landed in the real blob container, not just locally:

```
downloaded bytes from real Azure Blob Storage: 1101
starts with PNG magic (\x89PNG\r\n\x1a\n): True
```

**Then the actual test this milestone exists to pass:** deleted the local file
directly (`rm /tmp/m8_evidence/pdf-crop-....png`) -- simulating exactly what a
Container App revision replacement leaves behind, a fresh local disk with the old
crop nowhere on it -- and made a real HTTP request through `TestClient(app)` (the
full FastAPI dispatch path, not a direct method call) to `GET /api/v1/evidence/
{filename}`:

```
status: 200
content-length: 1101
is real png: True
```

The endpoint served the exact same 1101 bytes from blob storage, with zero local
copy present. This is the real, evidenced version of SPEC-M4/D-014's own OD-24
deferral -- not asserted from the CI-safe fake-container-client unit tests alone
(`tests/test_evidence_blob_persistence.py`), which prove the same logic but cannot,
by construction, catch a real Azure-side surprise (wrong auth flow, wrong content-
type handling, wrong RBAC scope) the way this run does.

## RBAC propagation, observed directly (for the record, faster this time)

Unlike D-018's ~8-10 minute wait for the Azure AI Search RBAC grant to take effect,
this storage-account role assignment was usable within roughly a minute of creation
(`az storage container create --auth-mode login` succeeded on first try). Recorded
for future reference, not as a general claim about Azure RBAC propagation timing --
this project has now observed both a slow and a fast case for the same underlying
mechanism.

## Cost posture

Standard LRS blob storage for a handful of PNG crops a few KB each: negligible,
well under $0.01/month at this project's demonstration scale. No always-on cost
consideration comparable to the Postgres Flexible Server (D-014) or even Azure AI
Search's free-tier ceiling (D-018) -- this is the cheapest of this project's four
Azure-managed resources by a wide margin.

## Not yet done, stated plainly

This baseline verifies the mechanism directly against the real Storage Account,
using a local script driving `AgentService`/`TestClient` with the real Azure
credential path -- it does **not** mean the live, deployed `armiem3-api` Container
App has been redeployed with `EVIDENCE_STORAGE_ACCOUNT_URL` set. `infra/bicep/
evidence.bicep` and the `apps.bicep`/`azure-deploy.yml` wiring are implemented and
Bicep-validated but not yet exercised through an actual `workflow_dispatch` run
against the live Container Apps environment -- that is a deliberate, separate,
owner-authorized step, the same pattern D-018 followed for Azure AI Search's own
deploy-workflow wiring (also still not exercised as of this report).
