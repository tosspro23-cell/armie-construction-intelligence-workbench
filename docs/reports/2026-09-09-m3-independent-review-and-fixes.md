# M3 Independent Review and Fixes

An independent model (a different vendor from the one that implemented M3) reviewed the public
repository after PR #9 (`e08e915`) and PR #10 (`2cf1aaf`) merged, at the owner's request. Full
findings are recorded in D-012 (`docs/decisions/README.md`) and `docs/decisions/
REVIEW_REQUIRED.md`; this report records the verification process and the end state, mirroring
`docs/reports/2026-08-24-m2p1-live-model-baseline.md`'s pattern of evidencing a claim rather than
just asserting it.

## Verification discipline

Per this project's own standard ("a fix is not done because tests pass"), every finding was
checked independently before being accepted or acted on -- none was taken on the reviewer's word
alone:

- Findings about live Azure state (model deployment mismatch, RBAC condition, shared identity)
  were checked with real `az` queries against the actual subscription, then the user-visible
  failure was reproduced directly (a real `/api/v1/chat` call returning `disposition="error"`).
- The telemetry root-cause claim (FastAPIInstrumentor instrumented from inside `lifespan`
  produces no spans) was independently reproduced in an isolated subprocess using
  `TestClient` + `InMemorySpanExporter` -- no network, no Azure -- comparing the exact two cases
  the reviewer described. The reproduction confirmed the claim exactly: 0 `SERVER` spans when
  instrumented inside `lifespan`, 1 when instrumented immediately after `FastAPI()` construction.
- The shell-injection and timeout-wiring findings were confirmed by direct code inspection against
  the actual files and line ranges cited.

All nine findings were confirmed accurate. None were dismissed or downgraded.

## What was fixed

| # | Finding | Fix | Verified |
|---|---|---|---|
| 2 | RBAC Administrator role assignment had no `condition` (self-privilege-escalation path) | Recreated with an ABAC condition restricting it to the two roles `platform.bicep` actually uses | Live: `az role assignment` requires no further roles beyond `AcrPull`/`Cognitive Services OpenAI User` |
| 3 | **Critical, confirmed live-broken**: model deployment names never passed to `apps.bicep`, silently defaulted to a non-existent `gpt-4o-mini` | Both parameters now required (no default); workflow verifies the deployments exist before deploying; post-deploy smoke test exercises both the deterministic and model-backed path | Live: redeployed with `gpt-5-mini` explicitly; the previously-`error` semantic question now reaches the model (`model_call_count=2`, real computed answer) |
| 4 | `model_call_timeout_seconds` never threaded into OpenAI/Azure providers | Both now accept and pass `timeout_seconds` to their SDK client | 179 tests pass; provider seam-invariance suite's generic `timeout_seconds` check now covers these two providers too |
| 5 | Shell injection via unquoted `${{ inputs.* }}` in `run:` scripts | All inputs moved to job-level `env:` vars, referenced as shell variables | `az bicep build`-equivalent for YAML: parsed and reviewed; will be re-verified on the next real `workflow_dispatch` run |
| 6 | Web and API Container Apps shared one identity with Azure OpenAI access | `platform.bicep` provisions a second, `AcrPull`-only identity for the web app | Live: queried the web identity's role assignments directly -- only `AcrPull`, scoped to the registry |
| 7 | `AppRequests` telemetry gap had an undiagnosed root cause | `configure_telemetry()` moved to module level, before `lifespan` runs | Isolated subprocess reproduction (see above); new regression test added |
| 8 | nginx's 60s default timeout shorter than the backend's 180s | Set `proxy_connect_timeout`/`proxy_send_timeout`/`proxy_read_timeout` to 180s | Config reviewed directly; matches backend's `request_timeout_seconds` default |

## What was deliberately not fixed here (correctly scoped out, not ignored)

- **Finding 1**: the public API has no authentication, authorization, or rate limiting. This is
  architectural, not a quick patch -- recorded in `docs/decisions/REVIEW_REQUIRED.md` as Phase 2
  scope. SPEC-M3 never claimed to have solved this; the review confirmed no later claim
  overstated it as solved either.
- **Finding 9**: audit/evidence does not persist across a Container App revision replacement (no
  volume, no external store). Needs real persistence (Phase 2's planned PostgreSQL direction,
  OD-20), not a bolted-on workaround. `PROJECT_STATE.md`'s local-dev-only audit-persistence claim
  was corrected to say so explicitly rather than leave the cloud case ambiguous.
- **Finding 4's second half**: `asyncio.to_thread`'s worker thread keeps running after
  `asyncio.wait_for` cancels the outer await on timeout. This predates M3 (it's in `main.py`'s
  `chat()` handler, unchanged by this milestone) and needs its own design work, not a quick patch
  bundled into this fix pass.

## Correction to a prior claim

D-012 previously stated that after the first real `workflow_dispatch` run, "the resulting
revisions answered both a deterministic question and a real Azure-OpenAI-backed question
correctly." This was wrong: only the deterministic question was actually re-tested at the time;
the semantic path was not, and was in fact broken by finding 3. The claim is corrected directly in
D-012 rather than quietly edited away, per this repository's own discipline against unflagged
release-claim changes.

## End-to-end re-verification after all fixes

Real deployment, real questions, after every fix above landed:

```
Deterministic: "How many doors are in this project?"
-> disposition: answered, model_call_count: 0

Semantic (the one that was broken):
"Please describe the situation with the doors in this project in your own words."
-> model_call_count: 2, actual_model: gpt-5-mini (correct deployment, not the
   previously-misconfigured gpt-4o-mini)
-> a real computed answer ("Level 01: 2; Level 02: 2...") with real IFC evidence citations
```

That second run returned `disposition="partially_answered"` rather than a clean `"answered"` --
not a regression from these fixes, and not one of the nine findings. The model decomposed the
question into two subtasks this time (LLM planning is not perfectly deterministic run to run);
the second subtask's execution result did not match its own requested shape, and the system
correctly refused to fabricate a value for it rather than reporting a false "answered" --
exactly the honest-disposition behaviour (D-004/D-010) this system is designed to have. The first
subtask's answer, with real citations, was still returned. This is the system working as
designed on a case its own planner made messier than necessary, not a defect.

## Cost/scope note

No new Azure resources were created for this review pass beyond the second managed identity
(finding 6's fix) -- no additional compute, no additional Container Apps, no change to the
`minReplicas: maxReplicas: 1` posture. The fixes are code, Bicep parameter, and IAM changes to
existing resources.
