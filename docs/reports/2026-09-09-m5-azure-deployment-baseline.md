# M5 — Azure Deployment Baseline (shared-secret auth, live)

Owner-authorized run against the same Azure subscription as the M3/M4 baselines, 2026-09-09.
Records the evidence that shared-secret auth actually works on the real deployed app, not a
claim without it, mirroring `docs/reports/2026-09-08-m3-azure-deployment-baseline.md`'s pattern.

## What was deployed

- A fresh `openssl rand -hex 32` secret, passed as `azure-deploy.yml`'s `api_shared_secret`
  input (masked in the run's own logs via `::add-mask::`) and `infra/bicep/apps.bicep`'s
  `apiSharedSecret` parameter.
- `armiem3-api`/`armiem3-web` redeployed with `API_SHARED_SECRET` now present as a Container
  Apps native secret (`secretRef`, not a plain env value); `DATABASE_URL` repeated unchanged
  from the M4 deployment (required by the "refuse to silently disable persistence" guard added
  in that milestone) so this run did not regress M4's persistence.

## End-to-end evidence

**The deploy workflow's own new smoke-test step**, run automatically as part of this
deployment, confirmed a request with no `Authorization` header returns `401` before the
authenticated smoke tests were allowed to run at all.

**Independently re-verified from outside the workflow**, directly against the live app, not
trusting the workflow's own report of its result:

```
POST /api/v1/chat, no Authorization header
-> HTTP 401

POST /api/v1/chat, Authorization: Bearer wrong-guess
-> HTTP 401

POST /api/v1/chat, Authorization: Bearer <the real secret>
-> HTTP 200, disposition: "answered", answer: "The project contains **4** doors."
```

All three cases match the design exactly: anonymous and wrong-key requests are rejected before
reaching any application logic; the correct key reaches the same working deterministic path
already proven in the M3/M4 baselines, unaffected by this milestone.

## CodeQL finding, found and resolved during this milestone (not deployment-related, but part of
the same verification pass)

`apiClient.tsx`'s `setStoredApiKey` was flagged by CodeQL (`js/clear-text-storage-of-sensitive-
data`, high) for storing the shared secret in browser storage, readable by any script on the
page. Investigated rather than dismissed reflexively: switched `localStorage` -> `sessionStorage`
(a real reduction in exposure window on a shared/public machine, no UX regression), then
dismissed the resulting alert with a written justification once it existed as a real alert on
`main` (GitHub's default-setup CodeQL does not honor inline `codeql[...]` suppression comments --
confirmed empirically, not assumed from docs, when a suppression comment left the PR check red
across two more pushes) -- alert #4, `dismissed_reason: "won't fix"`, reasoning: this secret's
security model is "possession = access" for every legitimate holder already (OD-28), so
client-side exposure to an XSS attacker grants nothing beyond what any intended holder already
has by design. This reasoning is specific to a shared secret with no per-holder differentiation
and would not justify the same dismissal for a real per-user credential.

## Corrections to prior claims

D-015, `PROJECT_STATE.md`, and `docs/decisions/REVIEW_REQUIRED.md`'s Finding 1 entry previously
stated this milestone's auth was implemented and verified locally/in CI but "not yet deployed to
the live Azure environment." That is now out of date as of this deployment; each is corrected in
place (flagged, not silently edited) rather than left to imply the gap still exists.

## Cost posture

No new Azure resources were created for this pass -- `API_SHARED_SECRET` is a Container Apps
native secret on the two Container Apps that already existed; no Key Vault, no additional
compute. Redeploying `armiem3-api`/`armiem3-web` is the same cost shape as every prior redeploy
in this project's history.
