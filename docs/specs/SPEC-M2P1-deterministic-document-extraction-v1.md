# SPEC-M2P1 — Deterministic Document Extraction

**Version:** v1 (draft for owner approval)
**Status:** Implementation-ready once the owner decisions in §11 are resolved.
**Authority:** This document is the authoritative contract for M2 Phase 1. It supersedes chat-level discussion.
**Target repository:** `tosspro23-cell/armie-construction-intelligence-workbench`
**Base branch:** `main` @ `0d11ed6` (merged M1)
**Implementation branch:** `feat/m2p1-deterministic-document-extraction`
**Canonical location:** `docs/specs/SPEC-M2P1-deterministic-document-extraction-v1.md`, committed as the first commit on the branch.
**Prior evidence:** `docs/reports/2026-08-23-pdf-extraction-diagnostic.md` (branch `chore/m2-pdf-diagnostic` @ `2df2253`).

### Relationship to M1.5

M1.5 (disposition collapse / sub-task result expression) is **deferred until after this
milestone** by owner decision. This specification therefore must not depend on sub-task-level
result structures that do not yet exist, and must not attempt to fix the disposition collapse.
The PDF path returns a single disposition today; keep it that way here.

### Relationship to M2 Phase 2

Phase 2 is cross-source reconciliation (BIM ↔ document). It is **out of scope here**. Phase 1
exists to make the document side deterministic and evidence-bearing first, because
reconciliation built on a probabilistic extractor inherits its error rate.

---

## 1. Objective

Replace the document tool's line-substring extraction strategy with **row- and
column-aware deterministic extraction that carries real region evidence**, and demote the
vision provider from primary path to genuine fallback.

## 2. Rationale

The diagnostic established that the current native path cannot succeed by construction, for
three compounding reasons, and that a fourth and deeper reason underlies all of them: PyMuPDF's
`get_text("text")` returns one line per table cell, so **no line contains both a board name and
its value**. Line-substring matching cannot perform table lookup regardless of the needle.
Consequently every document answer today comes from a vision model, even though the fixture has
a clean, fully machine-readable text layer.

This is a direct inversion of `D-001`: deterministic computation should be authoritative and
probabilistic interpretation should be bounded. On the document side, the system currently does
the opposite.

Fixing it also produces something the vision path cannot: extraction with exact `bbox`
coordinates, so evidence carries a real region rather than `bbox: None`.

## 3. Verified current-state assumptions

Reproduced 2026-08-23 against `main` @ `0d11ed6`. Re-verify before starting; **stop and report**
if any is false.

| # | Assumption | Status |
|---|---|---|
| B1 | `native_lookup` hardcodes `confidence = 0.55` on match, `0.0` otherwise (`analyzer.py:87`); `pdf_confidence_threshold` defaults to `0.75` (`config.py:40`); the gate at `graph.py:868` is therefore always true | verified |
| B2 | `query.field` is `plan.requested_field or state["question"]` (`graph.py:789`), and the heuristic router sets `requested_field` to the **raw question string** (`router.py:219`, `:233`). The needle is a whole sentence, never a field label. | verified |
| B3 | `native_lookup` never calls `target_field()`. That function's output is used only by `ambiguity_policy` and the vision prompts. | verified |
| B4 | `graph.py:809`'s `if board and field:` sends every board-and-field question **straight to vision**; `native_lookup` is never reached for `DB-L1-A` / `DB-L2-B` questions | verified |
| B5 | `target_board`'s regex `\b(?:SMDB\|DB)-[A-Z0-9]+(?:-[A-Z0-9]+)+\b` does not match `Panel-A`, one of the three boards in the fixture | verified |
| B6 | `ambiguity_policy` fires on a hardcoded field-name check (`field == "Total Connected Load" and not board`), not on observed document content | verified |
| B7 | `get_text("text")` returns one line per table cell; `get_text("blocks")` groups each table row into one block; `find_tables()` returns zero tables (no ruled lines) | verified |
| B8 | Word-level extraction yields exact left-aligned columns on this fixture: header and data share `x0` (Board 56.0, Connected Load 258.0, Diversity Factor 503.0); header row `y0=114.2`, first data row `y0=171.2` | verified |
| B9 | Fixture ground truth: DB-L1-A 18.50 / 0.75; DB-L2-B 26.00 / 0.65; Panel-A 44.50 / 0.70. Columns are exactly `Board`, `Connected Load (kW)`, `Diversity Factor`. No after-diversity column exists. | verified |

**Generality caveat, binding on implementation and documentation:** B8's exact alignment is a
property of this synthetic fixture. Real drawings will not be this clean. Implementation must use
tolerance-based clustering rather than exact coordinate equality, and no document produced by this
milestone may claim general table-extraction capability. The defensible claim is deterministic,
evidence-bearing extraction on documents whose structure has been characterized — not a general
extractor.

## 4. Allowed scope

### 4.1 Row- and column-aware extraction

1. Add a deterministic table-structure reader to `DocumentAnalyzer` that, from word-level
   extraction with coordinates: clusters words into rows by `y` within a tolerance; derives
   column bands from the header row's `x` extents; and assigns each cell to a column band, also
   within a tolerance. It must return, for each cell, the text and its `bbox`.
2. Rewrite `native_lookup` on top of that reader: locate the row whose identifier cell matches
   the requested record, locate the column whose header matches the requested field, return that
   cell's value **and its bbox**.
3. Header and identifier matching must be normalized (case, whitespace, unit suffixes such as
   `(kW)`) and must tolerate the requested label being a subset of the header. Do not require
   exact string equality.

### 4.2 Honest confidence

4. Remove the hardcoded `0.55`. Confidence must be derived from what the extraction actually
   established: exactly one matching row and exactly one matching column → high; multiple
   candidate rows or columns → low, with the ambiguity surfaced; no match → `0.0`.
5. The specific values and the threshold are set by **OD-11**. Whatever is chosen, a successful
   unambiguous deterministic extraction must clear `pdf_confidence_threshold`. A path that can
   never clear its own gate is the defect this milestone exists to remove.

### 4.3 Field identification

6. `requested_field` must carry a field identifier, not the raw question. Update the heuristic
   router accordingly (`router.py:219`, `:233`).
7. The semantic-planning path may also populate `requested_field`. Its values are model-generated
   and therefore untrusted: normalize and validate them against headers actually present in the
   document, and treat a non-matching value as an extraction miss, never as a guess.

### 4.4 Record identification

8. Replace or supplement `target_board`'s hardcoded regex per **OD-9** so that all three fixture
   boards, including `Panel-A`, are addressable.
9. `ambiguity_policy` must be driven by observed document content — how many rows or columns
   actually match — rather than by a hardcoded field name (B6). A question that unambiguously
   names a record must not receive "please specify the board."

### 4.5 Path ordering

10. Reorder `_execute_pdf` so deterministic extraction is attempted **first** and the vision
    provider is invoked only when deterministic extraction fails or is ambiguous. The current
    `if board and field:` short-circuit to vision (B4) must be removed.
11. When deterministic extraction succeeds, **no model call may occur** on that path. Assert this
    in tests via the call log.
12. Vision-path behaviour, prompts, and verification are otherwise unchanged. This milestone
    demotes vision; it does not modify it.

### 4.6 Evidence

13. Native-path evidence must carry the real `bbox` and `extraction_method="native_text"`, and
    must work with `crop_evidence` so a region image can be produced from a deterministic
    extraction.
14. Citations and `VerificationStatus` behaviour are unchanged (`D-003`).

### 4.7 Tests

15. Deterministic extraction tests against the fixture using B9's ground truth: all three boards ×
    both fields, asserting value, `bbox` presence, `extraction_method`, confidence above threshold,
    and **zero model calls**.
16. `Panel-A` regression tests specifically (B5), for both fields, asserting neither the
    ambiguity message nor a vision call.
17. Ambiguity tests: a question naming no record where multiple rows match → clarification driven
    by observed content; a requested field absent from the document (e.g. after-diversity, B9) →
    an extraction miss, never a fabricated value.
18. Fallback tests using `FakeModelProvider` (the M1 seam): deterministic extraction fails →
    vision is invoked → existing behaviour preserved. No live model.
19. All tests run with no Ollama, no network, no downloaded model. Suite runtime target under 45s.

### 4.8 Documentation

20. Update `PROJECT_STATE.md` and `README.md` to describe the document path as
    deterministic-first with vision fallback, including the §3 generality caveat stated plainly.
21. Add `docs/decisions/D-009` recording that deterministic document extraction is authoritative
    and vision is bounded fallback, as the document-side application of `D-001`.
22. Merge the diagnostic branch's report into this branch's history, or reference it by commit, so
    the evidence chain is contiguous.

## 5. Explicitly excluded scope

Cross-source reconciliation (Phase 2); any IFC-side change; adding the door/window schedule
fixture (**OD-12**); the disposition collapse (M1.5); multi-page documents; scanned or
OCR-required documents; ruled-table detection; layout ML; changing vision prompts or vision
verification; new runtime dependencies (PyMuPDF is already present); `graph.py` restructuring
beyond `_execute_pdf`'s ordering; auth, tenancy, persistence.

## 6. Affected surfaces

`apps/api/app/tools/document/analyzer.py`, `apps/api/app/agent/graph.py` (`_execute_pdf` only),
`apps/api/app/agent/router.py` (`requested_field` only), `apps/api/app/config.py` (threshold only),
`tests/`, `docs/decisions/`, `docs/specs/`, `docs/reports/`, `PROJECT_STATE.md`, `README.md`.

Zero-behaviour-change lint fixes required by the CI gate are permitted outside this list.

## 7. Invariants

1. Deterministic computation is authoritative; probabilistic interpretation stays bounded (`D-001`).
2. Answered results carry evidence, citations, and a `VerificationStatus` (`D-003`).
3. Disposition categories are not further collapsed (`D-004`); this milestone does not change them.
4. Fixtures remain synthetic (`D-005`).
5. No value is ever returned that was not read from the document. A miss is a miss.
6. Audit event names, stage names, and payload keys are unchanged; new fields may be added.

## 8. Acceptance criteria

1. All three boards × both fields answered deterministically, correct per B9, with real `bbox`,
   and **zero model calls**.
2. `Panel-A` answered for both fields.
3. An absent field yields a miss or clarification, never a fabricated value.
4. Vision fallback still works, proven with `FakeModelProvider`.
5. Full suite green; CI green across the Python matrix; lint gate clean.
6. No regression in the 122 existing tests.
7. Documentation states the generality caveat; no claim of general table extraction appears
   anywhere in the repository.
8. `git diff --stat` shows no changes outside §6.

## 9. Live-model baseline (owner-run, not agent-run)

**RESOLVED.** Live-model baseline collected locally 2026-08-24; see
`docs/reports/2026-08-24-m2p1-live-model-baseline.md`.

Parts 2–3 of the diagnostic could not run because the agent's sandbox has no Ollama (`which
ollama` → not found); the sandbox's loopback is not the owner's machine. Per **OD-10**, this step
runs on the owner's local machine with Ollama available, using a locally-executing agent session,
and is recorded as milestone evidence rather than as a gate on implementation:

- **before:** the nine diagnostic questions on `main` @ `0d11ed6`, twice, recording disposition,
  correctness, `extraction_method`, model-call count, and latency;
- **after:** the same nine on this branch.

Expected shape of the result: deterministic with zero model calls and region evidence, versus a
vision-dependent baseline that is reproducible at `temperature=0` (per `ollama_provider.py`), not
run-to-run variance. Report the actual numbers, including any case where the deterministic path
is worse.

## 10. Git, stop conditions

Branch from `main`. One PR against `main`. Do not merge. Merge authority is owner-only.

**Stop and report if:** any §3 assumption is false; tolerance-based clustering cannot reliably
separate the fixture's columns; deterministic extraction cannot clear the threshold without
weakening what confidence means; reordering `_execute_pdf` changes vision-path behaviour; an
existing test must be modified; scope drifts toward cross-source work or the disposition collapse.

## 11. Owner decisions (blocking)

All four resolved by the owner. Implementation proceeds on these resolutions; they are
binding, not recommendations.

| ID | Decision | Options | Resolution |
|---|---|---|---|
| **OD-9** | Record identification | (a) widen the `target_board` regex to cover `Panel-A`; (b) derive candidate record identifiers from the document's own first column and match the question against them | **RESOLVED: (b).** A regex encodes assumptions about naming conventions that no real project honours; document-derived vocabulary generalizes and is the more defensible architecture. The `target_board` regex is not widened. |
| **OD-10** | Live-model baseline | (a) required for acceptance; (b) owner-run evidence, not a gate | **RESOLVED: (b).** The implementing agent has no Ollama in its sandbox. §9's before/after run is owner-run evidence attached to the PR, not an acceptance gate; the implementing agent does not attempt, simulate, or block on it. |
| **OD-11** | Confidence scale and threshold | Set values such that unambiguous deterministic extraction clears the gate and ambiguity does not | **RESOLVED.** Unambiguous match = `0.95`; ambiguous = `0.4`; miss = `0.0`; `pdf_confidence_threshold` stays `0.75`. An unambiguous deterministic extraction clears its own gate (`0.95 >= 0.75`); an ambiguous one does not (`0.4 < 0.75`). |
| **OD-12** | Door/window schedule fixture | (a) add it in Phase 1 to prove the extractor on a second table shape; (b) defer to Phase 2, where it is needed for cross-source reconciliation | **RESOLVED: (b).** Phase 1 proves deterministic extraction on the existing document only. No fixture is added or modified under `demo_data/` in this milestone. |
