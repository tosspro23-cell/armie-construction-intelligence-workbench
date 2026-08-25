# Portfolio Updates

Plain-language summary of verified project progress, for external/portfolio use.
Entries reflect work that has been independently verified against the actual
code, not self-reported by an implementation agent. Full technical history:
see PROJECT_STATE.md and docs/decisions/.

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
