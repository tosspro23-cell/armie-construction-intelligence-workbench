# Portfolio Updates

Plain-language summary of verified project progress, for external/portfolio use.
Entries reflect work that has been independently verified against the actual
code, not self-reported by an implementation agent. Full technical history:
see PROJECT_STATE.md and docs/decisions/.

## 2026-09-19 to 2026-09-22 — Stress-testing the agent against two real buildings found and fixed seven real bugs

Rather than declaring the finding-investigation agent done once its own test suite passed, it was
pointed at two real, openly-licensed buildings (a residential duplex and a real university
building with 111 real door/window openings) and driven through repeated live investigations. This
found and fixed seven distinct real defects a synthetic fixture never surfaced: a real building's
own richly-annotated elements exceeding a cloud AI provider's per-request size limit; a model
correctly restating a real element's own identifying number being mistaken for an unverified
claim, in five different real phrasings; a model unable to look up one specific element precisely,
so it had to guess and sometimes guessed wrong; and a real GUID identifier's own digits being
misread as an invented number. Each fix was verified by first proving the exact bug live, then
confirming the fix live after redeploying — not by trusting the fix once it compiled. All seven
real findings across both real buildings now resolve correctly.

## 2026-09-16 to 2026-09-19 — Agent-assisted finding resolution (new capability)

A flagged discrepancy between the BIM model and a drawing schedule is no longer just reported once
and forgotten -- it becomes a persisted, human-reviewable item with its own status (open,
acknowledged, action required, resolved, re-verified/closed). An AI agent can be asked to
investigate one: it makes several real, independently-checked lookups against the actual model and
drawing, then proposes a specific conclusion with its own supporting evidence, for a person to
approve or reject. The agent never edits the source model or drawing itself, and a "re-check"
action always re-reads the real files fresh rather than trusting either the agent's or the human's
own say-so.

## 2026-09-16 — A tool-calling AI agent, benchmarked against the original design

Added a second way for the system to answer a question: instead of a fixed, hand-coded decision
path, a bounded AI agent chooses from a fixed toolbox of real, deterministic lookups across several
steps to work out an answer on its own. Benchmarked directly against the original approach on the
same real questions, including ones the original approach could never answer correctly regardless
of how many special cases were added to it. A verification step still checks the agent's own final
answer against what its tools actually found before it is shown as confirmed.

## 2026-09-15 — Real, openly-licensed building data added to the public demo

Two real buildings -- a genuine, previously-published residential duplex and a real university
building -- were added to the public demo alongside the original synthetic fixtures, each with
full attribution and license terms recorded, and confirmed to be exactly what their public source
repositories say they are. This is real portfolio evidence, not staged data: doing engineering
correctly on hand-built synthetic files is a much easier bar to clear than doing it correctly on a
real building's own, often messier, real-world data.

## 2026-09-08 to 2026-09-12 — Deployed as a real, running cloud application

The same codebase that runs locally for free now also runs as a real application on Microsoft
Azure: two independent web services, a real AI model, a real database, real file storage, and real
multi-project data isolation, all authenticating to each other with no passwords or API keys
anywhere in the code -- a cloud security practice, not a shortcut. Verified against the live,
running system itself, not just against local tests: a real conversation was confirmed to survive
the underlying server restarting, and two independent projects were confirmed unable to see each
other's data even when asked the exact same question with the exact same field names.

## 2026-08-25 — Cross-source reconciliation (new capability)

The workbench can now automatically check door and window counts and
dimensions between the BIM model and the engineering drawing schedule,
flagging exact matches, dimension mismatches, and records missing from
either source — with a full evidence trail and zero AI-model calls on the
deterministic path. Scope is intentionally narrow: only door/window
quantities are covered, and every other cross-source request is still
declined by design. Two real correctness issues (an execution-failure
case that could have been misreported as a false negative, and a
metadata field that overstated the response language) were found during
review and fixed before merge, not after release.

## 2026-08-25 — Explicit answer-status contract

Replaced a collapsed status model — where several different kinds of
"couldn't answer" all looked identical to the user — with a system that
distinguishes a system error, a request needing clarification, an
unsupported capability, and a genuine refusal. This is the foundation
the cross-source reconciliation feature above is built on: it is what
lets the system report "3 of 4 matched, 1 did not" instead of a single
flat yes/no.

## 2026-08-24 — Document reading rebuilt for speed and reliability

Root-caused why the system's PDF-reading path was structurally unable to
work reliably, then rebuilt it on a deterministic footing. Document
questions that used to take 5-35 seconds and two AI-model calls now
resolve in under 100 milliseconds at zero model calls on the successful
path — verified against live model runs before and after the change, not
just automated tests.

## 2026-08-23 — Reliability foundation

Fixed a real cross-version compatibility bug, built a 150+ test automated
verification suite spanning four Python versions, and established a
standing practice of documenting known limitations transparently in the
public repository rather than omitting them.
