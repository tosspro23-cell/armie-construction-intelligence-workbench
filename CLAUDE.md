# Contributor guidance

Read, in this order: `PROJECT_STATE.md` (milestone history), `AGENT_HANDOFF.md` (cold-start
guide and code map), `README.md`, `docs/architecture.md`, then `docs/specs/` (one file per
milestone) and `docs/decisions/README.md` (ADRs, `D-00n`) before changing this repository.
`docs/decisions/REVIEW_REQUIRED.md` lists known, tracked gaps -- check it before treating
something you notice as a new discovery.

## Non-negotiable invariants

- Preserve typed planning, capability gates, evidence, independent verification, and honest
  failure dispositions (`answered` / `partially_answered` / `clarification_required` /
  `unsupported` / `error` / `refused` -- see `docs/decisions/README.md` D-010).
- Use only the synthetic public fixtures in `demo_data/`; never commit private data, runtime
  traces, secrets, or machine-specific paths.
- Before a change, add or update a focused test and run
  `PYTHONPATH=apps/api python3 -m pytest -q` and `(cd apps/web && npm run build)`. Also run
  `ruff check --select F,E9,I,F401 apps/api tests` -- this is the **exact** flag set CI uses;
  a plain `ruff check .` uses a different default rule set and will miss things CI catches
  (this happened once already: an unsorted import passed local lint, failed CI).
- Keep product changes scoped and document release-claim changes. Do not claim a provider,
  deployment, or evaluation suite is validated without current evidence.
- Do not add a generic BIM query language, general cross-source joins, compliance reasoning,
  or long-term memory without an explicit, recorded owner decision (`OD-n`). One narrow,
  explicit exception exists today: SPEC-M2's door/window reconciliation pilot (OD-15) -- its
  existence does not authorize broadening cross-source capability further.

## How work gets committed

- **Spec first.** Any change beyond a trivial doc/typo fix starts with a spec document in
  `docs/specs/SPEC-<milestone>-<slug>-v1.md`, committed alone, before any code. Prior specs
  (`SPEC-M1-*`, `SPEC-M2P1-*`, `SPEC-M1.5-*`, `SPEC-M2-*`) are the template: Objective,
  Rationale, Verified current-state assumptions, Allowed scope, Explicitly excluded scope,
  Affected surfaces, Invariants, Acceptance criteria, Documentation requirements, Git/stop
  conditions, Owner decisions.
- **Two numbering systems, do not confuse them:** `OD-n` (owner decision) lives *inside* a
  spec document, scoped to that milestone's open questions -- numbering is global and
  continues across specs, not reset per file. `D-00n` (architecture decision record) lives in
  `docs/decisions/README.md`, one per durable technical decision, also globally numbered.
  **Next available: `OD-19`, `D-012`.** Check both files for the actual latest before using a
  number -- do not assume this file stays current.
- **Branch naming:** `feat/<milestone>` for a spec-driven milestone, `chore/<slug>` for
  tooling/dependency work, `docs/<slug>` for documentation-only changes.
- **One commit per logical subsection** of a spec's "Allowed scope" section, not one giant
  commit -- this is what makes the diff reviewable and, if something is wrong, revertible
  without losing unrelated work.
- **PR/merge is owner-only.** Commit and push freely on a feature branch. Do not open a PR or
  merge without an explicit, message-specific authorization -- a general instruction earlier
  in a conversation does not carry forward to a later, different change.

## Verification standard

A fix is not "done" because tests pass -- tests passing is necessary, not sufficient.
Demonstrate the defect actually existed before your change and is actually gone after it,
with a reproduction that does not rely on the same code path you just edited to prove itself
correct. Two examples already in this repo's history to match: `SPEC-M2P1`'s F12 rewrite
(rebuilt a stale test into a genuine repro of the miss case it claimed to cover, with a
call-sequence assertion, not just a final-value assertion) and the PR #6 review fix for a
disposition-fabrication bug (constructed an actually-corrupted PDF page, not a mock, to prove
the failure path really changes behavior). If you cannot construct an independent
reproduction, say so explicitly rather than presenting a fix as verified.

## Stop conditions -- report rather than improvise

Stop and report, do not proceed on your own judgment, if: an existing test's asserted
*behavior* needs to change (relocating or renaming a test is fine; changing what it asserts
is not, without a documented defect rationale in the commit message and, if it touches a spec's
own claims, in that spec's ADR); a task's stated affected-surfaces list turns out to be
incomplete once you're in the code; or a fix would require weakening an invariant listed
above or in a spec's own "Invariants" section.

## Known, currently-tracked gaps

See `docs/decisions/REVIEW_REQUIRED.md` for the current list (dead dependencies, response-
language coverage, routing-precision gaps, fixture/generator drift, etc.) before assuming
something you notice is new.
