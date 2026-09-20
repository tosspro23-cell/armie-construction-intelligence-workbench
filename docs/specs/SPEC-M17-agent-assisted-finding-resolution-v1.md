# SPEC-M17: Agent-Assisted Finding Resolution — v1

## 1. Objective

Add one new, optional step to SPEC-M11's already-shipped `EngineeringFinding` lifecycle: while a
finding sits in `ACTION_REQUIRED`, let a human ask V2's tool-calling agent to investigate the
discrepancy and propose a specific corrected value with rationale and citations, then require an
explicit human approval of that specific proposal before it counts toward `RESOLVED`. SPEC-M11's
own closed-loop guarantee — a fix is never trusted on anyone's word, the system re-reads the real
IFC/PDF sources before ever reaching `VERIFIED_CLOSED` — is completely unchanged: this milestone
only adds a *better-informed* way to arrive at `RESOLVED`, not a new way to leave it.

This is the first concrete instance of the "agent participates in a real business process, with a
human approval gate at the consequential step" pattern discussed with the owner (2026-09-18) as
the priority direction for extending V2 beyond single-turn question answering.

## 2. Rationale

SPEC-M11 proved the "detect → govern → re-verify" pattern end to end, but the `ACTION_REQUIRED →
RESOLVED` step today is a pure human action: a person has to already know what the correct value
is, fix it in the real source out of band, then click "Resolve." V2's own tool-calling loop
(`AgentService.invoke_v2`, SPEC-M16) already does exactly the kind of investigation this step
needs — read both sources' real values, check nearby context, propose a conclusion — but nothing
today connects it to a finding.

The design was deliberately scoped down from a more ambitious version discussed with the owner:
should the agent actually draft or regenerate the corrected artifact (a redlined drawing, an
updated IFC element) with a human only approving the artifact, or should the agent only identify
and propose *what* the correct value is, leaving the actual edit to a human, with the agent's role
limited to investigation and post-hoc re-verification? The owner-agreed answer (2026-09-18) is the
latter, for this pass: current AI tooling is not reliable enough at directly authoring engineering
artifacts to put that in an unsupervised or lightly-supervised path, and the real value V2
demonstrated in the D-059/D-061 benchmark was *interpretation over verified data*, not *artifact
generation* — this milestone stays inside that already-proven capability. See OD-52.

**Why not a general workflow engine.** The same conversation considered Azure Durable Functions,
the OpenAI Agents API, and Anthropic Managed Agents as off-the-shelf durable/human-in-the-loop
orchestration platforms. Rejected for this pass: OpenAI's and Anthropic's hosted agent platforms
each require moving off this project's existing Azure OpenAI (`gpt-5-mini`) deployment entirely —
a much larger, unrelated decision that should never be bundled into a workflow-shape choice.
Azure Durable Functions is a real, mature, Azure-native fit that does *not* require a model
change, but is a separate compute/deployment surface (Azure Functions, its own Task Hub storage,
a stricter deterministic-orchestrator programming model) — a genuine commitment this project's
single, well-scoped workflow instance does not yet justify. This milestone instead extends
SPEC-M11's already-shipped, hand-rolled state machine — proven, minimal, and running entirely
inside the existing FastAPI/Postgres stack this project's whole test suite already exercises. If a
second, differently-shaped workflow type is ever needed, that is the point to revisit Durable
Functions, not before.

## 3. Verified current-state assumptions

Checked directly against the current codebase, not assumed:

- `FindingStatus`, `EngineeringFinding`, `FindingHistoryEntry` (`apps/api/app/schemas/models.py`)
  and the transition table `_TRANSITIONS` + `validate_transition`/`resolve_reverify_outcome`
  (`apps/api/app/finding_workflow.py`) are shipped exactly as SPEC-M11 designed them — `acknowledge`,
  `start_action`, `waive`, `mark_false_positive`, `resolve` are the only legal human actions today;
  `reverify` has its own dedicated function, not a table entry, because its destination is decided
  by a fresh read, not chosen by the caller.
- `POST /api/v1/findings/{finding_id}/transition` and `POST /api/v1/findings/{finding_id}/reverify`
  (`apps/api/app/main.py:833`, `:855`) are live: the former calls `validate_transition` and turns
  `IllegalFindingTransition` into a `409`; the latter calls `AgentService.reverify_reconciliation_tag`
  (zero model calls, re-reads `_reconciliation_ifc_items`/`_reconciliation_pdf_items` filtered to
  one tag) and only then decides `VERIFIED_CLOSED` vs. bounced-back `ACTION_REQUIRED`.
- `AgentService.invoke_v2` (SPEC-M16, `apps/api/app/agent/graph.py`) already accepts a `question`
  plus `project_resources` and returns an `AgentResponse` with `answer_markdown`, `citations`,
  and `verification` (now four values post-D-062: `verified`/`unverified`/`failed`/
  `not_applicable`) — no new tool, dispatch, or verification logic is needed to invoke it from a
  new caller; this milestone adds a caller, not a capability.
- `EngineeringFinding` already carries `tag`, `ifc_width_m`/`ifc_height_m`/`pdf_width_m`/
  `pdf_height_m`, and `detail` (a human-readable summary already stating both sides' dimensions) —
  everything a `dimension_mismatch` investigation question needs to be built deterministically from
  the finding itself, with no free-text user input in the prompt (keeps the question reproducible
  and closes the obvious prompt-injection surface a free-text "ask the agent to fix this" box would
  open). Correction from an earlier draft of this spec: `EngineeringFinding` does **not** carry
  `entity_type`/`storey` (those exist only on the upstream `ReconciliationItem` it was seeded from,
  not copied forward) -- confirmed the hard way, by a live `AttributeError` when the implementation
  first tried to read them, not caught by re-reading the schema carefully enough beforehand. The
  question is built from `tag`/`detail`/both dimension pairs alone.
- `Citation`/`VerificationStatus` (`apps/api/app/schemas/models.py`) are already shared types
  between V1 and V2 responses — reusable as-is on a finding's stored proposal, no new evidence
  representation needed.
- `require_api_key` is already an app-wide dependency; every route this milestone adds is covered
  with no additional wiring, same as SPEC-M11 itself.
- `engineering_findings`/`engineering_finding_history` (`apps/api/migrations/0006_engineering_
  findings.sql`) are fully normalized, column-per-field tables — no generic JSON payload column
  exists to absorb a new nested field for free (`evidence_refs` is the one existing JSONB column,
  scoped to that one list field only). A new column on each table is therefore required for
  `pending_proposal`/`proposal_snapshot` (§4A) — this milestone does add a migration, unlike a
  schema-free store that could absorb new fields for nothing.

## 4. Allowed scope

**A. Schema.**
- `AgentProposal` (new, `apps/api/app/schemas/models.py`, next to `EngineeringFinding`):
  `proposal_id` (uuid), `proposed_width_m: float | None`, `proposed_height_m: float | None`,
  `rationale: str` (the agent's own `answer_markdown`), `citations: list[Citation]`,
  `verification: VerificationStatus`, `trace_id: str`, `generated_at: datetime`.
- `EngineeringFinding` gains `pending_proposal: AgentProposal | None = None`. Nullable, not a new
  `FindingStatus` value — a finding is either `ACTION_REQUIRED` with no proposal yet (today's
  exact behavior, unchanged) or `ACTION_REQUIRED` with one pending proposal awaiting a human
  decision; the status enum itself is untouched, so every existing SPEC-M11 test and status-based
  frontend branch keeps working unmodified.
- `FindingHistoryEntry` gains `proposal_snapshot: AgentProposal | None = None` — the immutable
  copy `approve_proposal` (§4C) writes into history, independent of the finding's own live
  `pending_proposal` field.
- `apps/api/migrations/0008_agent_proposal.sql` (new, additive-only, same hand-applied pattern as
  every migration before it): `ALTER TABLE engineering_findings ADD COLUMN pending_proposal
  JSONB;` and `ALTER TABLE engineering_finding_history ADD COLUMN proposal_snapshot JSONB;` — both
  tables are fully normalized, column-per-field (confirmed by reading `0006_engineering_
  findings.sql`; only `evidence_refs` uses JSONB today, scoped to that one field), so a new nested
  object genuinely needs a new column, not a schema-free slot. Update the matching CI migration-
  apply step in the same commit (D-024's own fix exists precisely because this step was missed
  once before).

**B. Propose-resolution endpoint. (Superseded 2026-09-19 by §13's chat-embedded redesign — kept
below as the original, as-built-then design for the historical record; do not implement this as
written.)**
- `POST /api/v1/findings/{finding_id}/propose-resolution` (`apps/api/app/main.py`), behind the
  existing app-wide `require_api_key`. Legal only when `status == ACTION_REQUIRED` and
  `finding_type == "dimension_mismatch"` (see §6 for why the other two finding types are excluded
  this pass) — otherwise `409`, matching `validate_transition`'s own error shape.
- New `AgentService.propose_finding_resolution(resources, finding) -> AgentProposal` method
  (`apps/api/app/agent/graph.py`), a thin, deterministic question-builder around the *existing*
  `invoke_v2`: constructs a question entirely from the finding's own stored fields (tag, entity
  type, storey, both sides' stored width/height) — never from free-text operator input — asking
  V2 to investigate which value is correct and why, using its existing tools
  (`get_element_properties`, `extract_pdf_field`, etc. — no new tool is added). Maps the resulting
  `AgentResponse` onto `AgentProposal` directly; does not retry or average multiple runs.
- This call does **not** change `status` or touch `pending_proposal`'s history — it only sets (or
  replaces) the finding's own live `pending_proposal` field. A second call before the first is
  acted on simply replaces the pending proposal; nothing about this is a state transition, so it
  is not validated against `_TRANSITIONS`.

**C. Two new transition-table entries** (`apps/api/app/finding_workflow.py`, alongside the existing
five — `reverify` stays its own dedicated function exactly as today):
- `approve_proposal`: legal only from `ACTION_REQUIRED` **and** only when `pending_proposal is not
  None` → `RESOLVED`. The `FindingHistoryEntry` this creates carries an **immutable copy** of the
  approved proposal's own fields (not just a pointer to the live `pending_proposal`, which a later
  `propose-resolution` call could overwrite) — the audit trail must show exactly what was approved,
  permanently, even after the live field changes. `pending_proposal` is cleared on the finding
  itself once approved.
- `reject_proposal`: legal only from `ACTION_REQUIRED` **and** only when `pending_proposal is not
  None` → stays `ACTION_REQUIRED` (a same-state transition, recorded in history like any other).
  Clears `pending_proposal`; an operator can call `propose-resolution` again or use the existing,
  untouched `resolve` action to close it manually instead.
- The existing `resolve` action is **not modified** — a human can still resolve a finding without
  ever invoking the agent, exactly as SPEC-M11 shipped it.

**D. Frontend (`apps/web/src/Findings.tsx`).**
- An "Ask agent to investigate" button, visible only when `status === "action_required"` and
  `finding_type === "dimension_mismatch"` and no `pending_proposal` exists.
- When `pending_proposal` is present: render its proposed value, rationale, and citations (reusing
  `DecisionStory.tsx`'s existing citation-card rendering), with a verification-status badge reusing
  D-062's exact `.verified`/`.unverified`/`.failed` styling — an `unverified` proposal must be
  visually distinguishable from a `verified` one at the point a human is asked to approve it, not
  just internally logged. "Approve" and "Reject" buttons replace the generic transition-button row
  while a proposal is pending.

**E. Tests + docs.**
- `tests/test_engineering_findings.py` (extends the existing table-driven suite): `propose_
  resolution` rejected with `409` when status isn't `ACTION_REQUIRED` or `finding_type` isn't
  `dimension_mismatch`; `approve_proposal`/`reject_proposal` rejected with `409` when no
  `pending_proposal` exists; `approve_proposal`'s history entry keeps the original proposal's
  values even after a later `propose-resolution` call overwrites the live field (proves the
  immutability claim in §4C, not just that the happy path works).
- `tests/test_v2_...` (new, alongside existing V2 test files): `propose_finding_resolution` against
  a real mutated-`Tag` fixture (the same isolation technique `tests/test_reconciliation.py` already
  uses) produces a proposal whose citations trace to the real sources — not asserted from a scripted
  fake answer alone.
- This spec document, committed alone, first. A new `D-063` decision-log entry recording this
  design and explicitly the OD-52 "no auto-approve, ever" decision. `PROJECT_STATE.md` M17 entry.

## 5. State machine (delta from SPEC-M11 §5)

```
                                    ACTION_REQUIRED
                                    |      ^      |
                    propose-resolution   reject_  approve_proposal
                        (sets              proposal  (requires
                     pending_proposal,      (clears   pending_proposal,
                      no transition)     pending_    clears it, ->
                                          proposal)    RESOLVED)
```

Everything else in SPEC-M11 §5 (`OPEN → ACKNOWLEDGED → ACTION_REQUIRED → RESOLVED →
VERIFIED_CLOSED`, `WAIVED`/`FALSE_POSITIVE` terminal, `reverify`'s own dedicated legality/
destination rule) is unchanged and not reproduced here.

## 6. Explicitly excluded scope

- **`missing_in_pdf`/`missing_in_ifc` finding types.** (Superseded 2026-09-19 — see §13/OD-54: both
  are now investigable, using their own dedicated question templates.) Originally excluded because
  these are presence/absence discrepancies, not a single wrong value to correct — "propose a
  resolution" was judged not a well-defined action for them without a demonstrated need first.
- **The agent never edits the real IFC file, PDF, or any drawing.** `propose_finding_resolution`
  is read-only over existing tools, identical to every other V2 question — it produces a proposed
  *number*, never a modified artifact. Applying a fix to the real source remains entirely a human/
  drafter action outside this system, exactly as it is today for a manually-resolved finding.
- **No auto-approval path, regardless of the proposal's own verification status.** Even a proposal
  whose `verification.status == "verified"` requires an explicit `approve_proposal` action — this
  is the deliberate (not autonomous-drafting) design point from §2/OD-52, not an oversight to
  optimize away later.
- **No new tool, dispatch branch, or capability added to V2's own tool surface.** This milestone
  adds a caller of `invoke_v2`, not a new thing V2 itself can do — no change to
  `apps/api/app/agent/tools.py` or the capability gate.
- **No durable workflow engine, no Azure Durable Functions, no OpenAI/Anthropic agent-platform
  adoption.** See §2 — this stays a synchronous REST addition on `FindingStore`, in-process with
  the existing FastAPI app, same as every SPEC-M11 route.
- **No generic multi-workflow-type abstraction.** This is scoped to `EngineeringFinding`
  specifically. A second, differently-shaped workflow (if one is ever needed) is a new spec, not
  a speculative generalization added here ahead of a second real case.
- **Rate-limiting `propose-resolution` itself.** The existing app-wide `rate_limit_requests_per_
  minute`/`rate_limit_global_requests_per_minute` settings (D-019/D-023) already cover every route
  including this one; no finding-specific quota is added. (Flagged for owner awareness in OD-53,
  not built here, given this deployment's own tight `gpt-5-mini` quota.)

## 7. Affected surfaces

`apps/api/app/schemas/models.py` (`AgentProposal`, `EngineeringFinding.pending_proposal`),
`apps/api/app/finding_workflow.py` (`approve_proposal`/`reject_proposal` table entries),
`apps/api/app/agent/graph.py` (`AgentService.propose_finding_resolution`, new — `invoke_v2` itself
unmodified), `apps/api/app/main.py` (one new route, two new legal actions on the existing
transition route), `apps/api/migrations/0008_agent_proposal.sql` (new), `.github/workflows/
ci.yml` (migration-apply step), `apps/web/src/Findings.tsx`, new tests,
`docs/decisions/README.md`, `PROJECT_STATE.md`.

## 8. Invariants

All SPEC-M11 invariants unchanged, in particular: no code path may transition a finding to
`VERIFIED_CLOSED` without an actual, fresh read of both sources (this milestone never touches
`reverify` or its own logic at all). New invariants this milestone adds:

- **No proposal is ever auto-approved.** `pending_proposal` being set never itself causes a status
  change — only an explicit `approve_proposal` or `reject_proposal` human action does, regardless
  of the proposal's own verification status.
- **`propose-resolution` is read-only.** It must never write to the real IFC/PDF sources, only to
  the finding's own `pending_proposal` field.
- **An approved proposal's audit record is immutable.** Once `approve_proposal` runs, the
  `FindingHistoryEntry` it creates must retain the exact proposal that was approved, unaffected by
  any later `propose-resolution` call on the same finding.

## 9. Acceptance criteria

- `propose-resolution` against a real `dimension_mismatch` finding (seeded from the same mutated-
  `Tag` technique `tests/test_reconciliation.py` uses) produces a persisted proposal with real
  citations tracing to the actual IFC/PDF sources — not a scripted fake answer accepted as proof.
- `propose-resolution` returns `409` for `missing_in_pdf`/`missing_in_ifc` findings and for any
  status other than `ACTION_REQUIRED`.
- `approve_proposal`/`reject_proposal` each return `409` when no `pending_proposal` exists, and
  are otherwise exercised through the full delta state machine in §5.
- A finding approved via `approve_proposal`, then given a *second* `propose-resolution` call,
  retains the *original* approved proposal's values in its history — checked directly against the
  stored history entry, not inferred from the endpoint's latest return value.
- The existing SPEC-M11 acceptance criteria (full lifecycle, illegal-transition coverage,
  re-verify's both branches) all still pass unmodified — this milestone must not regress any of
  them.
- `PYTHONPATH=apps/api python3 -m pytest -q` passes; `ruff check --select F,E9,I,F401 apps/api
  tests` clean; `(cd apps/web && npm run build)` passes.
- Deployed and live-verified against the real Azure app and a real Duplex/DigitalHub
  `dimension_mismatch` finding, per this project's established practice.

## 10. Documentation requirements

A new `D-063` decision-log entry (design summary, the delta state machine, and OD-52's "no
auto-approve, ever" decision recorded explicitly — this is exactly the kind of judgment call this
project's own decision log exists to make re-litigable only on purpose, not by accident).
`PROJECT_STATE.md` M17 entry.

## 11. Git / stop conditions

One commit per lettered subsection (A–E), this spec committed alone first. Branch:
`feat/m17-agent-assisted-finding-resolution`. Stop and report rather than proceeding if: OD-51/
OD-52 below are not confirmed before subsection B lands (B is the point this milestone's actual
agent-invocation scope becomes live); a `dimension_mismatch` investigation question cannot be
built deterministically from the finding's own stored fields alone without also requiring
free-text operator input (would reopen the prompt-injection surface this design deliberately
avoids — report back rather than silently accepting free text into the question); or extending
`propose-resolution` to `missing_in_pdf`/`missing_in_ifc` turns out to be needed to make this
useful in practice (out of this spec's scope — a real, demonstrated need is a reason for a
follow-up spec, not for silently broadening this one).

## 12. Owner decisions

- **OD-51 (needs owner confirmation).** Limiting `propose-resolution` to `dimension_mismatch`
  findings only in this first pass, explicitly excluding `missing_in_pdf`/`missing_in_ifc` (§6) —
  confirm this scope, or say now if the missing-from-one-side case is actually the more valuable
  target to start with.
- **OD-52 (owner-agreed 2026-09-18, in conversation; recorded here for the durable record).** The
  agent's role is limited to investigation and proposing a value — never drafting or regenerating
  an actual engineering artifact, and never auto-approving its own proposal regardless of
  confidence. A human approves every specific proposal by hand, every time. This is the considered
  answer to the "how autonomous should the agent be" question this milestone's own design raised —
  re-litigate only if the underlying product positioning changes, not by re-opening the question
  from scratch in a later session.
- **OD-53 (needs owner confirmation).** This deployment's `gpt-5-mini` quota is tight (10
  requests/10K tokens per minute, per SPEC-M16's own history) — `propose-resolution` adds a new,
  operator-triggered consumer of that same quota. Confirm the existing app-wide rate limits are
  sufficient, or specify a tighter, finding-specific guard if manual testing shows this feature
  competing meaningfully with normal chat usage for quota.
- **OD-54 (owner-agreed 2026-09-19, live-testing session; recorded here for the durable record).**
  Widen investigation coverage from `dimension_mismatch` only to all three SPEC-M11 finding types
  (`missing_in_pdf`, `missing_in_ifc` included) — supersedes OD-51/§6's original exclusion. See
  §13 for the trigger and what changed.

## 13. Amendment 2026-09-19 — chat-embedded architecture + scope widening

Two changes landed after subsections A–E were first built and tested, both driven by owner
feedback during live manual testing against the real Azure deployment — not a new spec, since
both are additive to this milestone's own already-approved intent (§1) rather than a new
capability boundary in the OD-15 sense.

**13.1 Chat-embedded investigation, replacing §4B's dedicated endpoint.** Manual testing of the
original `propose-resolution` design (§4B) surfaced a real product critique: rendering the
agent's reasoning inside the Findings tab, separately from the normal chat panel, read as a
second, disconnected "mini-agent" rather than the same V2 agent already visible elsewhere in the
workbench — undermining the exact "agent participates in a real business process" positioning
this milestone exists to demonstrate (§1). Owner decision: investigation now runs as a normal V2
chat turn instead.

- `POST /api/v1/findings/{finding_id}/propose-resolution` is **removed**. `ChatRequest`
  (`apps/api/app/schemas/models.py`) gains `finding_id: str | None = None`; when set, `_chat_v2`
  (`apps/api/app/main.py`) looks up and validates the finding (the same three guards §4B had —
  exists, `ACTION_REQUIRED`, a supported finding type; plus one new guard, the finding's
  `project_id` must match the thread's own bound project), builds the investigation question
  server-side (`AgentService.build_finding_investigation_question`, still built entirely from the
  finding's own stored fields, never from `request.question` — the same prompt-injection-safety
  property §4B had), and streams the resulting turn through the exact same `_v2_sse_stream` path
  as any other question — the Conversation panel, not the Findings tab, is where the reasoning now
  appears.
- `AgentService.propose_finding_resolution` (the old single method that called and fully consumed
  `invoke_v2` itself) is replaced by two static methods with no model-calling of their own:
  `build_finding_investigation_question(finding) -> str` (the question) and
  `build_finding_proposal(finding, final_response) -> AgentProposal` (maps an already-completed
  turn's `AgentResponse` onto a persistable proposal) — `_chat_v2` calls the first before
  `invoke_v2` runs and the second after it completes, so the frontend can stream the turn live
  instead of waiting for one blocking call to finish.
- `Findings.tsx` (§4D) is simplified to match: "Ask agent to investigate" hands off to
  `main.tsx`'s own `investigateFinding` (which calls the same `runV2Turn` the normal chat submit
  form uses, with `finding_id` set), and the pending-proposal card shows only a compact
  proposed-value/verification-badge summary with a note pointing back at the Conversation panel —
  it no longer renders the full rationale/citation list itself, since that already streamed
  visibly elsewhere.
- Live-verified against the real Azure `gpt-5-mini` deployment (not a scripted fake): a real
  investigation turn showed 6–7 real tool calls in the same Conversation panel used for normal
  chat — `get_element_properties`, `reconcile_doors_windows`, and multiple `extract_pdf_field`
  retries with different phrasings — reaching a confident, well-reasoned `answered` conclusion,
  which the human then approved through the normal Findings card, transitioning the finding to
  `RESOLVED`. This is the concrete evidence this redesign achieves what §1 set out to demonstrate.

**13.2 Two pre-existing V2 tool-layer bugs found and fixed during that same live-testing pass.**
Neither is part of this milestone's own allowed scope (§4) — both live in SPEC-M16's own V2
tool-calling infrastructure, used by every V2 question, not something this milestone added — but
both were the direct, reproducible cause of the agent "giving up too easily" that owner feedback
first flagged, so fixing them was a prerequisite to honestly claiming §1's demonstration works.
Recorded here, not as a new D-0xx (both are contained bug fixes, not durable design decisions),
with tests added alongside the fix in `tests/test_pdf_deterministic_extraction.py`:

- `extract_pdf_field`'s deterministic lookup (`DocumentAnalyzer.native_lookup`,
  `apps/api/app/tools/document/analyzer.py`) always defaulted to page 1 of a multi-page PDF when
  the model's own tool call carried no explicit page number — which it never can, since the tool's
  schema has no page parameter. The real door/window schedule in this project's demo fixture lives
  on page 2; every one of the model's own retries scanned the wrong page and got the same
  electrical-schedule miss every time, regardless of phrasing. Fixed with a narrow multi-page
  fallback: when no `page_hint` was given and the miss is specifically "this page's columns don't
  contain the requested field at all" (a new `miss_reason: "field_not_on_page"`), keep scanning
  forward until a page's columns do contain it — never for a caller that already named a page
  (reconciliation's own schedule reader), and never for a genuinely-absent-everywhere field or an
  ambiguous match, both of which report a different reason and stop at the first page as before.
- `extract_pdf_field`'s own tool-schema description (`apps/api/app/agent/tools.py`) actively
  misled the model: its one example (`'Panel-A connected load'`) embeds a record identifier into
  the `field` argument, but the deterministic column-matcher only ever matches `field` against a
  column *header* — a record name there can never match anything, on any page. Fixed by rewording
  the description to say the field is the column name only, with the record identifier going in
  `question` instead.

**13.3 Scope widened to all three SPEC-M11 finding types (OD-54).** After 13.1/13.2 confirmed the
architecture and tooling work for `dimension_mismatch`, the owner asked to widen coverage to
`missing_in_pdf`/`missing_in_ifc` too, superseding OD-51/§6's original narrower scope.

- `INVESTIGABLE_FINDING_TYPES` (`apps/api/app/finding_workflow.py`) replaces the old
  `dimension_mismatch`-only check in `_chat_v2` with `{"dimension_mismatch", "missing_in_pdf",
  "missing_in_ifc"}`.
- `build_finding_investigation_question` gained two more question templates, one per new finding
  type — each asks the genuinely different question those types call for ("is this a real
  omission, or does this element actually appear under a different tag/label," not "which of
  these two values is correct," which presupposes a value conflict these types don't have).
- `_extract_proposed_dimension` needed no code change: for these two types one side of the
  known-values pair is always `None` (e.g. a `missing_in_pdf` finding has real `ifc_width_m` but
  `pdf_width_m = None`), and its existing `expected is None -> never matches` rule already means
  the real side's value is proposed when the agent confirms it, never a fabricated value invented
  for the missing side.
- `Findings.tsx`'s button visibility gained the same three-type set, mirrored explicitly from the
  server's own `INVESTIGABLE_FINDING_TYPES` rather than reduced to "any type" — so a future fourth
  finding type does not silently start showing the button before its own question template and
  server-side guard exist.
- Live-verified against the real Azure deployment for both new types: a `missing_in_pdf`
  investigation (tag D04) correctly concluded a genuine PDF omission after multiple differently-
  worded schedule lookups and a vision-based page inspection, producing a proposal with the real
  IFC-side value; a `missing_in_ifc` investigation (tag W05) found IFC windows with matching
  dimensions and hypothesized (without fully ruling out that those windows were already accounted
  for under their own tags) that W05 might exist under a different tag — a genuinely interesting,
  disclosed limitation of open-ended multi-step reasoning: the proposal was correctly marked
  `unverified` in both the badge and the persisted `AgentProposal`, so a human reviewing it sees
  the caveat rather than a falsely confident claim. This is the intended fail-safe behavior
  (D-062), not a defect to chase before shipping.

**Net effect on §7 (affected surfaces):** add `apps/api/app/tools/document/analyzer.py`,
`apps/api/app/agent/tools.py` (13.2's fixes), and `apps/api/app/schemas/models.py`'s
`DocumentQueryResult.miss_reason` literal (widened, not narrowed). Remove the old
`propose-resolution` route from `apps/api/app/main.py`'s surface; add `ChatRequest.finding_id`
and the `_chat_v2`/`_v2_sse_stream` guard/persistence logic in its place.

**Net effect on §9 (acceptance criteria):** the "`propose-resolution` against a real
`dimension_mismatch` finding" criterion is satisfied instead through `POST /api/v1/chat` with
`finding_id` set, streaming into the same SSE path as any other V2 question; the "`409` for
`missing_in_pdf`/`missing_in_ifc`" criterion is inverted — both are now legal and each produces a
real persisted proposal, tested end-to-end in `tests/test_engineering_findings.py`
(`test_investigate_finding_is_now_legal_for_missing_in_pdf_and_missing_in_ifc`).
