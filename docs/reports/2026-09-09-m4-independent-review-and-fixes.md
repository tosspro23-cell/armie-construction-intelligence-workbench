# M4 Independent Review and Fixes

A second independent model (GPT) reviewed the pushed `feat/m4-postgres-persistence` branch at the
owner's request, auditing the actual remote commit rather than the working tree. Full findings are
recorded in the D-014 addendum (`docs/decisions/README.md`); this report records the verification
process and end state, mirroring `docs/reports/2026-09-09-m3-independent-review-and-fixes.md`'s
pattern of evidencing a claim rather than just asserting it.

## What this review caught that the first M4 pass didn't

Unlike SPEC-M3's independent review (which audited a real Azure deployment), this review caught
something purely mechanical but more basic: **GitHub's own CI for this branch was red**, on every
push, contradicting this session's own "186 tests pass" claim in D-014. That claim was true only
against a local `.venv` created before `apps/api/migrations/` existed -- `pip install -e` was
never re-run from a clean environment after that directory was added, so the defect it introduced
was invisible locally the entire time.

## Verification discipline

Per this project's own standard ("a fix is not done because tests pass"), every finding was
checked independently before being accepted or acted on:

- The packaging claim was checked by creating a genuinely fresh `python3 -m venv` and running
  `pip install -e 'apps/api[dev]'` against it -- reproduced the exact CI error locally, not just
  read from the log.
- The event-loop-blocking/inconsistent-state claim was checked with fault injection: fake
  `ConversationStore` implementations that raise or stall on command
  (`tests/test_chat_persistence_failure_handling.py`). Each of the three new tests was run against
  the pre-fix `apps/api/app/main.py` first (all three failed) and then against the fix (all three
  passed) -- not written to pass against the fix alone.
- The least-privilege claim was checked by design/code review (no live Azure Postgres Flexible
  Server exists to test the AAD-role-mapping mechanism against).
- The silent-persistence-disable and firewall-wording claims were checked by direct inspection of
  the workflow YAML and Bicep against the exact behaviour described.
- The final Python 3.9 break (found only because CI was re-run after the first four fixes, not
  predicted) was diagnosed directly from CI's own failure output, which named the exact two lines
  and error, then confirmed by checking that the other three Python-version jobs on the same
  commit passed unaffected -- isolating it to version-specific annotation evaluation rather than a
  logic bug, before writing the one-line fix.

All findings were confirmed accurate. None were dismissed or downgraded; two follow-on items
(exact Azure AAD function syntax, whether a CI Postgres service container fits the "no network
egress" policy) were explicitly left open rather than guessed at -- see "What was not fixed" below.

## What was fixed

| # | Finding | Fix | Verified |
|---|---|---|---|
| 1 | **Critical, confirmed CI-red**: `apps/api/migrations/` gave setuptools two flat-layout top-level package candidates, so `pip install -e apps/api` failed outright on every Python version | Explicit `[tool.setuptools.packages.find] include = ["app*"]` | Clean `python3 -m venv` + install succeeds; reproduced the failure first, confirmed the fix second |
| 2 | `chat()`'s initial context read ran outside error handling and directly on the event loop; final persistence could leave `app.state.requests` reporting "completed" during an actual failure | Every store call now via `asyncio.to_thread`; the initial read is inside error handling mapping to `Disposition.ERROR`; "completed" is recorded only after the final persistence attempt finishes | 3 new fault-injection tests, each independently confirmed to fail pre-fix and pass post-fix |
| 3 | The API's runtime managed identity was the Postgres Flexible Server's AAD administrator -- full db-admin privilege for a container that only needs three grants | AAD admin is now the CI/CD deploy principal; a new (unverified-live) migration script grants the API identity a plain, non-admin role with exactly `SELECT/INSERT/UPDATE` on `conversations` and `SELECT/INSERT` on `audit_events` | Bicep re-validated with `az bicep build`; the AAD-role-mapping mechanism itself is not live-verified (see below) |
| 4 | A redeploy leaving `database_url` blank silently reverted a persistence-enabled API to in-memory/JSONL mode | Pre-flight check refuses this unless a new `allow_disabling_persistence` input confirms it | Code inspection; mirrors the existing "verify model deployments exist" pre-flight pattern |
| 5 (P2) | Firewall-rule comment overstated network isolation; `docker-compose.yml` bound Postgres to all interfaces; `_TokenRefreshingPool` closed its old pool before confirming the replacement worked; no connect/pool/statement timeouts existed; the persistence smoke test only checked `disposition != "error"` | Comment corrected against Microsoft's own docs; compose bound to `127.0.0.1`; pool-swap reordered (build new, then close old); `connect_timeout`/`statement_timeout`/pool-wait timeout added everywhere; smoke test now requires `"answered"` and the fixture's known correct count | Re-verified against a real local Postgres after each change; no regression in the already-verified persistence path |
| 6 | **A second, distinct CI break**, found only because CI was re-run after fixes 1-4: two `str \| None` annotations added to `main.py` during fix 2 crashed on Python 3.9 | `from __future__ import annotations` added to `main.py`, matching the existing convention in `config.py`/`postgres_store.py` | A real GitHub Actions run passed on all five jobs afterward, checked via `gh run view --json jobs` |

## What was not fixed here (correctly scoped out, not ignored)

- **`pgaadauth_create_principal_with_oid`'s exact syntax** (finding 3's migration script) is not
  verified against a live Azure Database for PostgreSQL Flexible Server -- that extension function
  doesn't exist on the local docker-compose Postgres everything else here was checked against, and
  no Flexible Server was deployed this session. Flagged explicitly inside the script itself, with
  a link to check current Microsoft documentation before running it for real.
- **Whether CI should run a real Postgres service container** for the currently `TEST_DATABASE_URL`-
  gated tests is a genuine policy question (does pulling `postgres:16-alpine` from Docker Hub fit
  `ci.yml`'s "no network egress beyond package registries" clause?), not decided unilaterally in
  either direction -- tracked in `docs/decisions/REVIEW_REQUIRED.md`.

## End-to-end re-verification after all fixes

```
Clean install:        python3 -m venv + pip install -e 'apps/api[dev]'  -> succeeds
Local test suite:     189 passed, 3 skipped (from that same clean venv)
Lint:                 ruff check --select F,E9,I,F401 apps/api tests    -> clean
Real GitHub Actions:  lint, backend x{3.9,3.10,3.11,3.12}, frontend      -> all success
                       (https://github.com/tosspro23-cell/armie-construction-intelligence-workbench/actions,
                        branch feat/m4-postgres-persistence)
Real local Postgres:  docker compose up postgres; migration applied;
                       TEST_DATABASE_URL-gated tests pass; manual close()
                       smoke check passes; timeouts/pool-swap-ordering
                       changes re-verified with no regression
```

## Cost/scope note

No Azure resources were created or modified for this review pass -- every fix is code, Bicep
parameter/resource restructuring (not yet deployed), a GitHub Actions workflow change, or a local
Postgres check via `docker compose`. `infra/bicep/data.bicep` remains undeployed pending the
owner's explicit cost go-ahead, unchanged from the first M4 pass.
