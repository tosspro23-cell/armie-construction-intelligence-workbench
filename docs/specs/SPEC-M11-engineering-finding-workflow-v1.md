# SPEC-M11: Engineering Finding Workflow — v1

## 1. Objective

Promote the existing IFC↔drawing reconciliation output from a one-shot response into a
persisted, human-reviewable `EngineeringFinding` object with an enforced lifecycle — detected,
reviewed, acted on, resolved, and re-verified against the actual sources before being closed —
without broadening reconciliation's own join scope (still door/window only, OD-15) or adding any
new cross-source shape.

## 2. Rationale

M2 (D-011, OD-15/16/17/18) built a narrow, deterministic, zero-model-call reconciliation that
already produces item-level `matched`/`dimension_mismatch`/`missing_in_pdf`/`missing_in_ifc`
results with evidence and independent verification. Today that result is a one-shot chat answer:
it exists for exactly one response, then is gone — nothing persists a discrepancy, tracks whether
anyone has looked at it, or proves a claimed fix actually holds against the real sources. Raised
directly by the owner (2026-09-14), citing an outside consultant's interview feedback: the
highest-value next step is not another query capability, but turning an already-detected
engineering discrepancy into a governed business object a human can act on and the system can
independently re-check — "unstructured information → workflow → automation," not "AI answers one
more question type."

**What this milestone is not.** It is not a general issue tracker, not a new cross-source join
capability, and not a claim of real user identity/authorization (this project has no per-user
login yet — D-016's `X-Session-Id` is a per-tab correlation token, not an authenticated actor;
every "human" transition in this milestone is attributed to that token, explicitly documented as
a stand-in, not a real identity claim). The one thing this milestone must prove, and the thing
that actually differentiates it from a chatbot: **a finding marked "resolved" by a human is not
trusted on the human's word — the system re-reads the actual IFC and PDF sources before closing
it.**

## 3. Verified current-state assumptions

Checked directly against the current codebase, not assumed:

- `AgentService._synthesize_reconciliation_response` (`apps/api/app/agent/graph.py:782`) already
  computes `matched`/`dimension_mismatch`/`missing_in_pdf`/`missing_in_ifc` per item, joined on
  `Tag`/`Mark`, zero model calls, with real evidence (`Evidence`/`Citation`) and an independent
  verifier result. `_reconciliation_ifc_items`/`_reconciliation_pdf_items`
  (`apps/api/app/agent/graph.py:702`, `:734`) are the two deterministic reads this milestone's
  re-verify action reuses unchanged.
- `ReconciliationItem` (`apps/api/app/schemas/models.py:42`) already has every field this
  milestone's `EngineeringFinding` needs to be *seeded from* (`tag`, `entity_type`, `storey`,
  both sides' width/height, `status`, `detail`).
- `AgentResponse.reconciliation_items` (`apps/api/app/schemas/models.py:298`) is already returned
  by `/api/v1/chat` on every reconciliation response — confirmed the frontend never renders it
  anywhere today. This milestone's frontend work closes that gap as a side effect, not as a
  separate task.
- `ConversationStore`/`AuditStore` (`apps/api/app/persistence/`) already establish the exact
  pattern this milestone's `FindingStore` follows: a `Protocol`, an `InMemory*` default
  implementation, and a `Postgres*` opt-in implementation sharing `PostgresAuditStore`'s existing
  Managed-Identity conninfo path (`apps/api/app/persistence/postgres_store.py`). `ServiceContainer`
  (`apps/api/app/services.py:121`) injects both via constructor-default factories
  (`conversation_store_factory`, `audit_store_factory`) — this milestone adds a third,
  `finding_store_factory`, the same way.
- Migrations are additive-only SQL files with no framework (`apps/api/migrations/0001`-`0005`).
  D-024 fixed a real gap where CI's migration-apply step silently only ran `0001` — the workflow
  step must be updated for this milestone's new migration in the same commit that adds it, not
  after.
- `require_api_key` (`apps/api/app/security.py`) is already an app-wide FastAPI dependency
  (`app = FastAPI(dependencies=[Depends(require_api_key)])`) — every new route this milestone adds
  is covered with no additional wiring.

## 4. Allowed scope

**A. Persistence.**
- `apps/api/app/persistence/finding_store.py`: `FindingStore` Protocol
  (`upsert_from_reconciliation`, `get`, `list_for_project`, `append_transition`),
  `InMemoryFindingStore` (default), `PostgresFindingStore` (opt-in via the existing `DATABASE_URL`).
- `EngineeringFinding`, `FindingStatus` (str enum), `FindingHistoryEntry` in
  `apps/api/app/schemas/models.py`, next to `ReconciliationItem`.
- `apps/api/migrations/0006_engineering_findings.sql` (findings + history tables), plus the
  matching `.github/workflows/ci.yml` migration-apply step update in the same commit.
- `ServiceContainer.finding_store_factory` (`apps/api/app/services.py`), matching the existing
  `conversation_store_factory`/`audit_store_factory` constructor-injection pattern exactly.

**B. Auto-creation from reconciliation, not a separate "promote" action.**
- Hook in `AgentService._finalize` (`apps/api/app/agent/graph.py`), after the response is fully
  constructed, when `result.get("reconciliation_items")` is non-empty. `_synthesize_reconciliation_
  response` itself stays a pure, side-effect-free computation — unchanged.
- For every item with `status != "matched"`: `finding_store.upsert_from_reconciliation(...)`,
  keyed on `(project_id, tag, finding_type)` — an existing open (`OPEN`/`ACKNOWLEDGED`/
  `ACTION_REQUIRED`) finding for that exact key is updated in place (new `detail`/observed values,
  `updated_at` bumped), never duplicated by re-running the same question twice.
- Severity assigned deterministically at creation, one fixed rule: `dimension_mismatch` →
  `medium`; `missing_in_pdf`/`missing_in_ifc` → `high`. Not a model call, not configurable this
  milestone.
- A `matched` item belonging to a tag with an existing open finding does **not** auto-close it
  here — closing only ever happens through the explicit re-verify action in §C, so a human
  always sees the transition rather than a finding silently vanishing between chat turns.

**C. Transition + re-verify API**, all behind the existing app-wide `require_api_key`:
- `GET /api/v1/findings?project_id=...&status=...` — list, optionally filtered.
- `GET /api/v1/findings/{finding_id}` — one finding, full append-only history.
- `POST /api/v1/findings/{finding_id}/transition` — body `{action, note?}`, validated against the
  state table in §5; records the caller's `X-Session-Id` as `last_actor_session_id` and appends a
  `FindingHistoryEntry`. An illegal transition is a `409`, never a silent no-op.
- `POST /api/v1/findings/{finding_id}/reverify` — legal only from `RESOLVED`. Re-runs
  `_reconciliation_ifc_items`/`_reconciliation_pdf_items` filtered to this finding's own `tag`,
  compares fresh values against the stored finding, and transitions to `VERIFIED_CLOSED` (now
  matches) or bounces back to `ACTION_REQUIRED` with an updated `detail` (still mismatched) — the
  actual "closed-loop" claim, proven by re-reading the real sources, not by trusting the human's
  own "I fixed it" click.

**D. Frontend Findings panel.**
- New `apps/web/src/Findings.tsx`: a "Findings" tab alongside BIM Model/Drawing/Viewer Snapshot in
  `main.tsx`'s workspace tabs. Lists findings for the active project with status badges; a detail
  view shows evidence (reusing `AuthedImage`/citation-fact rendering patterns already in
  `DecisionStory.tsx`), only the transition buttons legal from the finding's current status, and
  "Re-check now" when `RESOLVED`.
- The reconciliation answer's own Evidence step (`DecisionStory.tsx`) gains a compact per-item
  status table sourced from `AgentResponse.reconciliation_items` — closing the gap in §3 where
  this field has been returned, unrendered, since M2.

**E. Tests + docs.**
- `tests/test_engineering_findings.py`: the state machine's legal/illegal transitions
  (table-driven); upsert-not-duplicate across two reconciliation runs on the same mismatch;
  re-verify's both branches, using the same IFC-`Tag`-edit isolation technique
  `tests/test_reconciliation.py` already uses to mutate one source between detection and
  re-verify, proving re-verify actually re-reads rather than trusting stored state.
- This spec document (committed alone, first). `D-032` decision-log entry recording the design
  and the `X-Session-Id`-as-actor simplification. `PROJECT_STATE.md` M11 milestone entry.
  `docs/decisions/REVIEW_REQUIRED.md` gains an entry for the no-real-identity gap this milestone
  inherits rather than solves.

## 5. State machine

```
OPEN ---------------> ACKNOWLEDGED --------> ACTION_REQUIRED --------> RESOLVED
 |        (human)          |     (human)                                  |
 |                          |--> WAIVED (human, terminal)                 |--(system, reverify, match)--------> VERIFIED_CLOSED (terminal)
 |                          |--> FALSE_POSITIVE (human, terminal)         |--(system, reverify, still mismatched)--> ACTION_REQUIRED (detail updated)
 |--> ACKNOWLEDGED (human, direct path from OPEN)
```

`status` is created directly as `OPEN` — no persisted `DETECTED` row; the instant before
persistence is not a state any human ever acts on, a deliberate simplification of a six-state
proposal down to the states that actually matter, not a silently dropped one. `WAIVED`/
`FALSE_POSITIVE`/`VERIFIED_CLOSED` are terminal — no further transitions accepted from them
except through a fresh auto-creation (§B) if reconciliation detects the same tag+type again.

## 6. Explicitly excluded scope

- **Real user identity/authorization.** `last_actor_session_id` is `X-Session-Id`, not a login —
  stated plainly in the model's own field name and this spec, not implied otherwise. Entra
  ID-backed per-user authorization remains a separate, unbuilt future step (`README.md`'s own
  "Known limitations").
- **Configurable severity rules, custom finding types, or any type beyond the three reconciliation
  already produces.** One fixed rule (§4B); revisit only against a real need, not speculatively.
- **Any new cross-source join shape.** Reconciliation's own scope (OD-15: door/window only) is
  completely unchanged by this milestone — findings are downstream of an existing computation,
  never a new one.
- **Outbound integration** (ServiceNow/Jira/SAP/Autodesk Construction Cloud/Power Automate). This
  milestone's REST surface (`GET`/`POST /api/v1/findings/*`) is the integration contract; no
  outbound connector is built this pass.
- **Automatic resolution.** The system never transitions `ACTION_REQUIRED → RESOLVED` on its own —
  only a human claims a fix is in place; only the system's own re-read decides whether that claim
  holds.
- **The Dataset Pack proposal** (a real, openly-licensed IFC plus ARMIE-generated companion
  documents) — sequenced by the owner as a separate, later initiative, contingent on its own
  licensing/technical go-no-go spike, not part of this milestone.

## 7. Affected surfaces

`apps/api/app/schemas/models.py` (new types), `apps/api/app/persistence/finding_store.py` (new),
`apps/api/app/services.py` (`ServiceContainer.finding_store_factory`), `apps/api/app/agent/
graph.py` (`_finalize`'s new post-response hook; `_reconciliation_ifc_items`/`_reconciliation_
pdf_items` reused, not modified), `apps/api/app/main.py` (four new routes), `apps/api/migrations/`
(new `0006`), `.github/workflows/ci.yml` (migration-apply step), `apps/web/src/Findings.tsx`
(new), `apps/web/src/main.tsx` (new tab), `apps/web/src/DecisionStory.tsx` (reconciliation
per-item table), new tests, `docs/decisions/README.md`, `PROJECT_STATE.md`,
`docs/decisions/REVIEW_REQUIRED.md`.

## 8. Invariants

All prior invariants unchanged, including OD-15's reconciliation-scope boundary (this milestone
adds no new join shape) and D-010's honest-disposition discipline (a finding's `status` is never
advanced by anything other than an explicit human transition or the system's own re-read of the
real sources — never inferred, never defaulted forward). New invariant this milestone adds: **no
code path may transition a finding to `VERIFIED_CLOSED` without an actual, fresh read of both
sources for that finding's own tag** — a re-verify that trusted the originally-stored values
instead of re-reading would defeat the entire point of this milestone.

## 9. Acceptance criteria

- A reconciliation question against `demo` creates real, persisted findings for every
  non-`matched` item; re-asking the identical question does not duplicate them.
- The full lifecycle is exercised end to end: `OPEN → ACKNOWLEDGED → ACTION_REQUIRED → RESOLVED`,
  then a re-verify after actually editing the underlying IFC `Tag`'s width to match the PDF
  results in `VERIFIED_CLOSED` — and a re-verify with the mismatch left in place instead bounces
  back to `ACTION_REQUIRED` with an updated `detail`, not a false `VERIFIED_CLOSED`.
  This is checked directly against a mutated fixture, not asserted from the endpoint's return
  value alone, per this project's own verification standard.
- Every listed illegal transition in §5 is rejected with `409`, verified by a table-driven test,
  not only the legal path.
- `PYTHONPATH=apps/api python3 -m pytest -q` passes.
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `(cd apps/web && npm run build)` passes.
- Deployed and live-verified against the real Azure app, following this project's established
  practice of a real post-deploy check, not just a passing test suite.

## 10. Documentation requirements

`D-032` (design summary, the state machine's exact transition table, and the `X-Session-Id`-as-
actor simplification). `PROJECT_STATE.md` M11 entry. `docs/decisions/REVIEW_REQUIRED.md` entry for
the no-real-identity gap. `README.md`'s Roadmap item on this exact topic is promoted from "idea
under evaluation" to a real capability once shipped.

## 11. Git / stop conditions

One commit per lettered subsection (A–E), this spec committed alone first. Branch:
`feat/m11-engineering-finding-workflow`. Stop and report rather than proceeding if: the owner
decision in §12 is not confirmed before subsection B lands (auto-creation is the point where this
milestone's scope becomes live, not just a schema on disk); re-verify cannot be made to actually
distinguish "re-read confirms fixed" from "re-read still shows the mismatch" in testing (would
mean the core closed-loop claim is unsound — do not ship a re-verify that doesn't actually
re-verify); or extending this to any cross-source shape beyond door/window reconciliation turns
out to be needed to make the demo compelling (out of this spec's and OD-15's scope — report back
rather than silently broadening it).

## 12. Owner decisions

- **OD-44 (needs owner confirmation).** Persisting and workflow-governing reconciliation's
  already-computed output does not itself broaden cross-source capability under OD-15's existing
  restriction (no new join shape is added; this milestone is downstream of an existing,
  unchanged computation). This spec's own reading is that OD-15 permits this without a new
  capability decision — flagged here for explicit owner sign-off rather than assumed silently,
  per this project's own discipline around cross-source scope.
- **OD-45 (owner-confirmed 2026-09-14, in conversation).** Build the Finding workflow now, ahead
  of the Dataset Pack proposal, per the owner's own explicit sequencing; the Dataset Pack proceeds
  only after its own licensing/technical go-no-go spike, not in parallel.
