# Decision Log

This page records the small set of decisions that define the public reference release. It is not an approval ledger for future product work.

## D-001 — Deterministic facts remain authoritative

IFC counts, storey grouping, controlled quantity resolution, unit conversion, and arithmetic run in Python/IfcOpenShell. Models interpret a request and explain verified results; they do not calculate or invent BIM facts.

## D-002 — One typed plan contract and a capability gate

Heuristic and bounded semantic planning converge on `QueryPlan`/`MultiQueryPlan`. Cross-field validation and the capability gate run before controlled tools. A valid plan may still be explicitly unsupported; it must not be silently downgraded to an unrelated operation.

## D-003 — Evidence precedes verification and presentation

Tool results produce `Evidence` and `Citation` locators. Independent deterministic, evidence, or visual checks produce `VerificationStatus`. The UI exposes grouped stages and keeps raw payloads in a collapsed developer view.

## D-004 — Honest failure is a feature boundary

Ambiguous questions clarify; valid unsupported capabilities refuse with an explanation; provider/transport failures are errors; cancellation and deadlines are terminal states. The system must not turn missing evidence into a confident answer.

## D-005 — Public repository uses synthetic fixtures only

`demo_data/` and committed screenshots are synthetic/public-safe. Supplied assignment/customer material, derived crops, runtime traces, credentials, and local model caches are excluded. This repository is a local reference workbench, not a security or privacy boundary.
