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

**Resolved, see D-010.** `error`, `clarification`, and `unsupported` previously collapsed into a single `refused` disposition on the semantic-planning failure path (through M2P1); M1.5 makes them distinct terminal dispositions, mapped to their actual underlying cause.

## D-005 — Public repository uses synthetic fixtures only

`demo_data/` and committed screenshots are synthetic/public-safe. Supplied assignment/customer material, derived crops, runtime traces, credentials, and local model caches are excluded. This repository is a local reference workbench, not a security or privacy boundary.

## D-006 — Frontend dependency resolution pins `three@0.149.0`

`apps/web/package.json` pinned `three@0.155.0` while `web-ifc-three@^0.0.126` peer-requires `three@^0.149.0`. On a clean checkout this made both `npm install` and `npm ci` fail (`ERESOLVE`), so the only working install path was `--legacy-peer-deps`, which is not a reproducible build.

The viewer (`apps/web/src/IfcViewer.tsx`) only uses core Three.js APIs (`Scene`, `PerspectiveCamera`, `WebGLRenderer`, `BoxGeometry`, orbit controls) that are stable across 0.149–0.155; it does not import `web-ifc` or `web-ifc-three` (see M1.5 candidate below). M1 pins `three@0.149.0` and `@types/three@^0.149.0`, regenerates `package-lock.json`, and verifies `npm ci` (no flags), `tsc -b`, and `vite build` all pass. Production bundle size drops 619 kB → 585 kB as a side effect of resolving to the older, smaller `three` release; no functional or rendering change was made.

M1 does not migrate the IFC/browser dependency stack. `web-ifc`, `web-ifc-three`, and the copied WASM/worker assets are retained unmodified; whether they are needed for a future client-side IFC parsing path or are removable dead weight is deferred to M1.5 (`docs/decisions/REVIEW_REQUIRED.md`).

## D-007 — Provider injection seam and the deterministic fake as the probabilistic test boundary

Before M1, `ModelProvider` instances were constructed inline inside `AgentService` graph methods (`get_text_provider` / `get_vision_provider` called directly at the call site), so no planner, repair, or escalation path could be exercised without a live Ollama daemon. This made every reliability claim about the probabilistic path unfalsifiable.

M1 adds `model: str` to the `ModelProvider` Protocol (both concrete providers already satisfied it structurally; this only makes the existing dependency explicit) and injects provider **factories** — not provider instances — through `ServiceContainer`, defaulting to the existing `get_text_provider` / `get_vision_provider`. Provider selection logic stays centralized in `providers/factory.py`; the seam changes who calls the factory, never how it chooses. `llm_provider` handling, timeout propagation, bounded-repair/escalation ordering, and audit field semantics are unchanged (verified by `tests/test_provider_seam_invariance.py`, §4.7).

`tests/fakes/fake_provider.py` provides `FakeModelProvider`, a scripted, no-network implementation of `ModelProvider` used to drive every failure-path eval (F1–F12). It is the only mechanism used to exercise the probabilistic path in tests; production `app.providers.factory` globals are never monkeypatched as a substitute, because that would leave the real injection seam untested.

## D-008 — Canonical disposition contract

M1 makes the mapping from failure condition to response disposition explicit and normative (`docs/specs/SPEC-M1-reliability-foundation-v1.md` §4.3): transport failure/timeout/unavailability → `error`; unparseable output after bounded repair → `error`; unsupported capability → `unsupported`/refusal with rationale; ambiguous request → `clarification`; failed verification → not `answered`; client cancellation → `cancelled` with no conversation-context mutation; deadline exceeded → a timeout state distinct from `error`. No branch may emit a numeric or factual claim without evidence, citations, and a passing `VerificationStatus`. F1–F12 (§4.5) test this contract directly; any implementation divergence found while testing is a defect finding, not a reason to adjust a test.

### Current conformance — resolved, see D-010

Through M2P1, production did **not** fully satisfy this contract: `error`, `clarification`, and `unsupported` all surfaced as disposition `refused` on the semantic-planning failure path, rather than as the three distinct categories above. M1.5 (D-010) fixes this: `AgentService._unsupported_subresult` now maps the actual underlying cause (a raw `state["planner_error"]`, `plan.intent == "clarification"`, or a genuinely unsupported capability) to the correct disposition instead of hardcoding `refused`, and `_refuse`'s existing `error`/`unsupported` distinction is now the only disposition it ever emits (its own historical `refused` fallback was, on inspection, always the "unsupported capability" case). `tests/test_failure_path_evals.py`'s F2, F5, F6, F8, and F9, plus the new `tests/test_disposition_contract.py`, assert the corrected behaviour. See D-010 for the full mechanism.

## D-009 — Deterministic document extraction is authoritative; vision is bounded fallback

This is the document-side application of D-001. Before M2 Phase 1, `DocumentAnalyzer.native_lookup` could never succeed by construction: it hardcoded `confidence = 0.55` on a substring match against `pdf_confidence_threshold`'s default of `0.75` (`docs/reports/2026-08-23-pdf-extraction-diagnostic.md`), and `graph.py`'s `_execute_pdf` additionally short-circuited straight to vision whenever both a board and a field were heuristically detected, so `native_lookup` was never even reached for the two of the fixture's three boards that matched `target_board`'s regex. Every document answer came from the Ollama vision model, even though the fixture has a clean, fully machine-readable text layer — a direct inversion of D-001 on the document side.

M2P1 (`docs/specs/SPEC-M2P1-deterministic-document-extraction-v1.md`) replaces line-substring matching with row- and column-aware extraction: words are clustered into rows by `y` and into column bands, derived from the header row's `x` extents, by `x` — both within a tolerance, not exact coordinate equality. `native_lookup` locates the row whose first-column identifier (document-derived, per OD-9 — `target_board`'s regex is not widened) matches the question and the column whose header matches the requested field, and returns that cell's real text and bbox. Confidence reflects what was actually established (OD-11): unambiguous match `0.95` (clears the unchanged `0.75` threshold), ambiguous `0.4`, a field genuinely absent from the document `0.0`. `_execute_pdf` now attempts this path first; the vision flows (board-localized and generic) are otherwise unmodified and run only as a fallback when deterministic extraction fails or is ambiguous.

**Generality caveat, stated plainly:** the row/column clustering is characterized against this fixture's clean, left-aligned layout (tolerance-based, not exact-coordinate — see `docs/specs/SPEC-M2P1-deterministic-document-extraction-v1.md` §3). This is **not** a general table-extraction capability. No claim is made that it works on an arbitrary drawing, a multi-page document, a scanned/OCR-required document, or a ruled-line table (`find_tables()` returns zero tables on this fixture — there are no ruled lines to detect in the first place). A different document layout needs its own characterization, following the same diagnose-then-implement pattern this milestone used, before this path can be trusted on it.

`tests/test_pdf_deterministic_extraction.py` covers all three boards × both fields (value, bbox, `extraction_method`, confidence above threshold, zero model calls — asserted via `FakeModelProvider`'s call log, not just by inspection), the `Panel-A` regression, content-driven ambiguity/miss handling, and the vision-fallback path. `tests/test_failure_path_evals.py`'s F12 scenario was rewritten (a scoped, authorized exception to the "no existing test may be modified" default) once its original question turned out to name both a real field and a real record unambiguously under the new deterministic-first ordering — the new architecture correctly answering it, not a regression; see the SPEC-M2P1 PR for the full trace.

## D-010 — Explicit disposition taxonomy; the D-004/D-008 collapse is fixed

M1 documented (D-004, D-008) that `error`, `clarification`, and `unsupported` all collapsed into a single `refused` disposition on the semantic-planning failure path: `AgentService._route` set `state["planner_error"]` on a raw transport/parsing failure, but only the `_refuse` node ever read it, and `_refuse` was reachable only via the cross-source-join guard's conditional edge — never via the `route → execute_multi` path that actually sets `planner_error`. Separately, `AgentService._unsupported_subresult` hardcoded `disposition="refused"` for any `source="unsupported"` subplan regardless of cause. The M2P1 live-model baseline independently found a structurally identical instance of the same disease in the PDF fallback path (Q7): a structural record-miss and a genuinely ambiguous match shared one confidence-threshold gate with no reason carried forward.

SPEC-M1.5 (`docs/specs/SPEC-M1.5-disposition-contract-v1.md`) fixes both, owner-authorized under OD-13/OD-14:

- **Taxonomy (OD-13).** `Disposition` gains an explicit `UNSUPPORTED` member (`schemas/models.py`), alongside the existing `ANSWERED`, `PARTIALLY_ANSWERED`, `CLARIFICATION_REQUIRED`, `REFUSED`, `ERROR`, `TIMEOUT`, `CANCELLED`. `_unsupported_subresult` now maps a `source="unsupported"` subplan to its actual cause instead of hardcoding `refused`: `state["planner_error"]` set → `error`; `plan.intent == "clarification"` (the "issues persisted after repair/escalation" branch in `_route`) → `clarification_required`; otherwise (a genuinely unsupported capability — cross-source join, nearest-space search, an unsupported entity type) → `unsupported`. `_refuse`'s existing `error`/`refused` split becomes `error`/`unsupported` for the identical reason: its only live trigger (the cross-source-join guard) is a genuinely unsupported capability, not a generic refusal. On inspection, every live `refused` emission in `graph.py` was one of these three cases — none was ever a distinct fourth thing — confirming this was exactly the documented collapse, not a new design question. `REFUSED` remains in the taxonomy as a defensive fallback with no live trigger today (`tests/test_disposition_contract.py` proves and documents this directly, rather than leaving it as an unverified claim).
- **Q7 fix (OD-14).** `DocumentQueryResult` gains `miss_reason: Literal["no_matching_record"] | None`, set by `native_lookup` only when zero candidate rows match the question (a structural miss vision cannot resolve either, since it's a "which record did you mean" ambiguity, not a document-legibility one). `_execute_pdf`'s confidence gate checks this before invoking vision, routing a structural record-miss straight to `clarification_required` at zero model calls. Every other below-threshold case (field absent, multiple field or record matches) is unchanged and still falls through to vision.
- **Cross-source-join guard consolidation.** Four call sites shared one detection function (`cross_source_join_requested`) but each independently constructed a rejection response; three were unreachable dead code (`AgentService._resolve_context` is the single live authority — it runs before any other node and diverts before `route`/`execute_multi` can reach the other three), and one of the three crashed if ever invoked directly (`heuristic_multi_plan`'s `MultiQueryPlan(subplans=[])` violated the schema's `min_length=1`). The crash is fixed; the redundant-but-correct, independently-unit-tested checks in `heuristic_plan` and `_synthesize_multi_response` are deliberately retained (not deleted — removing them would change those functions' own directly-tested behaviour without a defect rationale) and now carry inline comments stating their reachability status. Product behaviour is unchanged: cross-source joins are still refused (now `disposition="unsupported"`) after this milestone.

`tests/test_disposition_contract.py` covers every taxonomy member against the real code path that produces it, the Q7 zero-model-call regression, and the reachability proof for the guard consolidation. `tests/test_failure_path_evals.py`'s F2/F5/F6/F8/F9 assertions were updated to the corrected dispositions (a documented defect fix, not a silent adjustment — see their updated docstrings), and `test_defect_heuristic_multi_plan_crashes_on_cross_source_join` was rewritten to assert the fixed, graceful behaviour.

## D-011 — A narrow, first-class exception to the cross-source-join refusal: door/window reconciliation

D-010 (and the M1.5 diagnostic behind it) established that `cross_source_join_requested` refuses *every* cross-source shape unconditionally — by design, until this milestone. Per explicit owner decision (SPEC-M2, OD-15), M2 opens one narrow, specific door: comparing door/window quantities between the IFC model and the engineering-drawing schedule. Every other cross-source shape (electrical panel↔room area, and any future shape not yet decided) remains refused, unchanged, regression-tested (`tests/test_reconciliation.py`).

**Gate carve-out, single definition (OD-15).** A new, deliberately conservative `cross_source_reconciliation_requested(question)` requires a door/window entity term AND a reconciliation-intent verb (compare/verify/reconcile/check/match/cross-check/consistent, or the Chinese equivalents) AND a schedule/drawing reference term — all three. `cross_source_join_requested`'s single definition returns `False` whenever this new detector matches, per the same discipline D-010 established: no individual call site is touched, so all four (one live, three retained-dead) inherit the carve-out automatically and cannot drift relative to each other again. `tests/test_reconciliation.py` proves the carve-out is scoped precisely — at least two non-reconciliation cross-source questions are still refused after it lands, and a separate test shows the carve-out is load-bearing (not vacuous) against a phrasing that would otherwise match the old join guard too.

**Execution does not reuse the generic per-subplan contract.** `reconciliation_plan` (`router.py`) builds a two-subplan `MultiQueryPlan` tagged `intent="reconciliation"` for audit purposes, but neither subplan is independently executable through the existing single-entity-type IFC executor or the existing single-record/field PDF `native_lookup` executor: the IFC side must cover both `IfcDoor` and `IfcWindow` together, and the PDF side must read every row of a table rather than one question-driven lookup. `AgentService._execute_multi` routes `intent="reconciliation"` to a dedicated path (`_synthesize_reconciliation_response`) instead, which reads Tag and `Qto_*BaseQuantities` directly from the already-open `ifcopenshell` model and every row of the schedule's new page 2 via `DocumentAnalyzer._read_table` — the exact, unmodified M2P1 extraction path, not a new technique. `_route` returns this plan immediately, before the generic `canonicalize_multi_plan`/`validate_multi_plan` contract runs, because that contract would misread the IFC subplan's intentionally-absent single `entity_type` as a validation failure and silently downgrade the plan to a clarification request.

**Join key: `Tag`, not `Name` or `GlobalId` (OD-18, owner-explicit).** `Tag` is `IfcElement`'s schema-intended human-facing mark/identifier field — the same semantic role a real drawing's Mark column plays — and was unset on all 8 fixture instances before this milestone; populating it (no new entities, no geometry change) is fixture work this milestone owns, verified isolated by diffing (`tests/test_reconciliation.py::test_ifc_tag_edit_touched_only_the_eight_targeted_attributes`; see that test's docstring for why a whole-file regenerate-and-diff was tried and rejected in favor of a targeted, deterministic assertion against the committed fixture). `Name` is a tool-generated display label, not a stable identity field; `GlobalId` is an internal 22-character identifier that never appears on a real drawing.

**Result representation and disposition mapping (OD-16, owner-explicit; mechanism per Project Control recommendation).** A reconciliation that executes to completion — both sides were read, regardless of any individual item's status — is `answered`, with a new structured `reconciliation_items: list[ReconciliationItem]` field on `AgentResponse` carrying the item-level breakdown (`matched` / `dimension_mismatch` / `missing_in_pdf` / `missing_in_ifc`, at ±0.01m tolerance independently per dimension). This is a deliberate, explicit decision to keep `partially_answered`'s existing meaning — one subplan itself failing to execute — unconflated with "the two sources disagree," which is a content outcome, not an execution failure. `partially_answered`/`error` remain reserved for a genuine execution failure (for example the schedule page cannot be parsed at all).

**Correction (PR #6 review):** the first version of this dedicated join did not actually honor that last sentence. `_reconciliation_pdf_items` returned an empty mapping both when the schedule page genuinely had zero matching rows and when `_read_table` returned `None` because the page could not be read at all — indistinguishable to the caller — so a real PDF-read failure was reported as `answered`/`verification: passed` with every IFC item fabricated as `missing_in_pdf`. Fixed: `_reconciliation_pdf_items` now raises when the table structure cannot be read at all (distinct from a legitimately empty table), and `_execute_multi`'s reconciliation branch catches this the same way `_execute_ifc` catches its own tool failures — audited, mapped to `disposition="error"`, `reconciliation_items` left empty, with an honest message naming what failed (`tests/test_reconciliation.py::test_pdf_read_failure_is_reported_as_error_not_answered`). Separately, `_synthesize_reconciliation_response`'s answer text is an English-only template; it now always reports `response_language="en"`, regardless of what `reconciliation_plan` detected from the question, rather than overclaiming a language the text isn't actually written in — real localized reconciliation templates are follow-up work (`docs/decisions/REVIEW_REQUIRED.md`).

## D-012 — Provider portability extended to Azure OpenAI (Managed Identity only); Container Apps over AKS for Phase 1

SPEC-M3 (`docs/specs/SPEC-M3-azure-vertical-slice-v1.md`) is the first milestone to deploy this system outside a local workstation. Two decisions from that process are durable enough to record here rather than leave scattered across commit messages.

**Provider portability, not a new architecture (OD-23, owner-explicit).** `AzureOpenAIProvider` (`apps/api/app/providers/azure_openai_provider.py`) implements the existing `ModelProvider` protocol exactly like `OllamaProvider`/`OpenAIProvider` do; `providers/factory.py` gained one additional `llm_provider == "azure"` branch, per D-007's discipline that selection logic stays centralized there. The only new invariant this milestone adds: **no API-key code path exists for Azure.** `AzureOpenAIProvider` authenticates exclusively via `DefaultAzureCredential` (a user-assigned managed identity in the deployed environment, selected through the `AZURE_CLIENT_ID` environment variable — see `infra/bicep/apps.bicep`), so no Azure secret is ever stored in application config, a container image, or an IaC template. A `client_factory` injection seam (mirroring D-007's provider-factory pattern) lets `tests/test_azure_openai_provider.py` exercise both the success and the token-acquisition-failure path with zero live Azure calls.

**Container Apps, not AKS, for Phase 1 (OD-19, owner-explicit).** Azure Container Apps was chosen over AKS for this milestone's compute target specifically because it gives containerization, revisions, managed identity, and platform-managed ingress/TLS with materially less operational surface than a Kubernetes control plane the owner would otherwise have to run before this system does anything cloud-native at all. AKS is deferred to a later phase and must be justified by a capability Container Apps cannot provide (a KEDA/queue-triggered worker for batch IFC/PDF processing jobs was the concrete candidate raised during planning) — not a second deployment of the same web application, which would demonstrate nothing Container Apps hadn't already proven.

**Found during this milestone, not part of either decision above:**

- `apps/api/app/config.py`'s `Settings.model_config` computed its `.env` file location as `Path(__file__).resolve().parents[3]`, which raised `IndexError` and crashed the API container at import time, because the container's `WORKDIR /app` + `COPY apps/api /app` layout puts this file one directory shallower than the local dev checkout always has. Fixed by replacing the fixed-index `.parents[3]` lookup with four chained `.parent` calls (`_default_env_file`), which resolves to the identical path for local dev and never raises regardless of depth. `tests/test_config_env_file_resolution.py` reproduces the pre-fix expression's own `IndexError` directly against the container's real path shape, independent of the fix. This is the concrete instance of the Dockerfile-unvalidated gap `PROJECT_STATE.md` had already flagged as a known limitation — found by actually building and running the image, not assumed.
- `azure.identity.aio`'s async credential needs `aiohttp` as its HTTP transport; `azure-identity` does not pull it in automatically. `AzureOpenAIProvider` failed with `"aiohttp package is not installed"` the first time a Managed Identity token was actually requested against a live Azure OpenAI resource. Fixed by adding `aiohttp` to `apps/api/pyproject.toml`.
- Both `OpenAIProvider` and `AzureOpenAIProvider` originally called `client.responses.create(...)` (the newer Responses API). Against a real Azure OpenAI resource this returned 404 regardless of API version — verified directly with a raw REST call: `/openai/deployments/{deployment}/responses` and `/openai/responses` both 404 "Resource not found", while `/chat/completions` returned 401 PermissionDenied for an unauthorized caller in the same probe, proving the route exists and this was never an auth problem. Both providers were switched to `client.chat.completions.create(...)`.
- Chat Completions with `response_format={"type": "json_schema", ..., "strict": True}` then failed with a 400 (`"'additionalProperties' is required... to be false"`), because `QueryPlan.filters` is a free-form `dict` field — OpenAI's Structured Outputs strict mode does not support arbitrary-key dict/mapping types at all, and `QueryPlan` is a protected, must-remain-stable contract (`AGENT_HANDOFF.md`). Rather than restructure that contract to fit strict mode, `strict` was dropped entirely: non-strict JSON-schema guidance plus this system's existing `model_validate_json` (and its semantic-repair retry path for a mismatch) is already the same reliability model the local Ollama provider uses, so this makes OpenAI/Azure consistent with it rather than inventing a third approach.
- Container Apps' Envoy-based ingress edge terminates TLS for **internal-only** apps too — `infra/bicep/apps.bicep`'s `apiInternalUrl` originally used `http://`, which produced Azure's own "stopped or does not exist" 404 page (a platform-routing rejection, not a connection error) instead of reaching the container. Fixed to `https://`. Once on `https://`, nginx's `proxy_pass` then needed `proxy_ssl_server_name on` (nginx does not send the TLS SNI extension by default, which the ingress edge needs to route to the correct internal app) and `proxy_set_header Host $proxy_host` (forwarding the web app's own `$host` instead of the API app's confused the edge's own routing) — both in `apps/web/default.conf.template`.
- `apps/api/Dockerfile` never bundled `demo_data/`, so a deployed API container could reach Azure OpenAI and reason about a question but always failed at tool execution with an honest "IFC file not found" (D-004 working correctly, just not a useful demo). `demo_data/` is synthetic, public-safe fixture data (`SECURITY_AND_DATA.md`) already served from the same files in local dev, so bundling it into the image changes no data-boundary invariant.

All five were found only by actually deploying to a real Azure subscription and driving real requests through the real path — none is reachable through `FakeModelProvider`, which exists specifically to test the *seam*, not real transport/schema/platform-routing behaviour. End-to-end proof after all fixes: a natural-language question against the real deployment produced 2 real Azure OpenAI model calls, a correctly-decomposed `MultiQueryPlan`, and a final `disposition="answered"` response with real IFC evidence citations and `verification.status="passed"`.

**Two more, found running `.github/workflows/azure-deploy.yml` for real (not reachable by writing/compiling the YAML — only an actual GitHub-triggered run surfaces platform-specific auth/authorization behaviour):**

- `azure/login@v2`'s OIDC token had a subject of
  `repo:tosspro23-cell@231253569/armie-construction-intelligence-workbench@1334122128:ref:refs/heads/main` —
  GitHub's org/repo *names* with their immutable numeric IDs appended via `@`, not the plain
  `repo:owner/name:ref:...` format most OIDC federation tutorials show. The federated
  credential created with the plain-name subject failed with `AADSTS700213: No matching
  federated identity record found`. Fixed by recreating the federated credential with the
  exact subject GitHub's own token actually presents (copied from the error message, not
  guessed) — one credential per trusted branch (`main`, and the feature branch this milestone
  was developed on).
- The GitHub deploy app's service principal had `Contributor` on the resource group (scoped
  deliberately, not subscription-wide), which is not sufficient to create the
  `Microsoft.Authorization/roleAssignments` resources `platform.bicep` defines (`AcrPull`,
  `Cognitive Services OpenAI User`) — `Contributor`'s built-in role definition explicitly
  excludes `Microsoft.Authorization/*/write`, by Azure's own design, as a privilege-escalation
  guardrail. Fixed by adding `Role Based Access Control Administrator`
  (`f58310d9-a9f6-439a-9e8d-f62e7b41a168`, verified against the real subscription, not assumed)
  scoped to the same resource group — deliberately not `Owner`, to keep the deploy identity's
  privilege bounded to exactly what this pipeline does (manage resources, manage role
  assignments on them) rather than the unrestricted superset `Owner` would grant.

With both fixed, `workflow_dispatch` ran end to end on `main` for the first time (build, push,
deploy) in 3m7s. **Correction, found by an independent review (below): the claim originally
written here — that the resulting revisions "answered both a deterministic question and a real
Azure-OpenAI-backed question correctly" — was wrong.** Only the deterministic question was
actually re-tested after that run; the semantic/model-backed path was not, and was in fact
broken (see the independent-review section below, finding 3). The CI/CD path's build/push/
deploy mechanics were genuinely proven working by that run; the claim that the *deployed
application* was fully verified afterward was not, and should not have been written without
re-checking it.

## Independent review of PR #9/#10 (2026-09-09): nine findings, all verified before acting on any of them

The owner had an independent model (a different vendor, not Claude) review the public repository
after PR #9 and PR #10 merged. Per this project's own verification discipline, every finding was
checked against the real code and the real live Azure state before being accepted — some in a
literal, isolated reproduction — rather than trusted or dismissed on the reviewer's word alone.
All nine were confirmed accurate. Fixed in `fix/spec-m3-independent-review-findings`:

1. **No authentication or rate limiting on the public API** (via the web app's reverse proxy).
   Confirmed: any internet caller can invoke `/api/v1/chat`, consuming Azure OpenAI quota, or
   query/cancel another user's request by ID. Genuinely architectural, not a quick patch — **not
   fixed in this pass, tracked in `docs/decisions/REVIEW_REQUIRED.md` as a Phase 2 item**, and
   the milestone's own claims were checked to confirm none of them overstated this as solved.
2. **The GitHub deploy identity's `Role Based Access Control Administrator` role assignment had
   no `condition`**, meaning a compromised deploy identity could grant itself (or anything else)
   `Owner` on the resource group — confirmed directly (`condition: null` in the live assignment).
   Fixed: recreated with an ABAC condition restricting it to creating/deleting role assignments
   for exactly the two roles `platform.bicep` actually uses (`AcrPull`, `Cognitive Services
   OpenAI User`) — verified live that it can no longer assign anything else.
3. **CRITICAL, confirmed live-broken**: `azure-deploy.yml` never passed
   `azureOpenAiTextDeployment`/`azureOpenAiVisionDeployment` to `apps.bicep`, so Bicep's own
   defaults (`gpt-4o-mini`) silently deployed against a model that does not exist on this
   account (only `gpt-5-mini` does). Confirmed by querying the live Container App's env vars
   (`gpt-4o-mini`) against the account's actual deployment list (`gpt-5-mini` only), then by
   reproducing the user-visible failure directly: `disposition="error"`, `model_call_count=0`
   on the real deployed app. This is *why* the paragraph above needed correcting — the workflow
   run that "succeeded" never actually exercised the model path it silently broke. Fixed: both
   parameters are now required (no default) on `apps.bicep` and as workflow inputs; the workflow
   verifies both deployments exist on the account before deploying anything; a new post-deploy
   smoke test exercises both the deterministic and the model-backed answer path and fails the
   workflow if either comes back `error` — the direct fix for how this went unnoticed.
4. **`Settings.model_call_timeout_seconds` was never threaded into `OpenAIProvider`/
   `AzureOpenAIProvider`** — only `OllamaProvider` ever respected it; the other two silently used
   `openai`'s own SDK default (600s). Confirmed by inspection (no `timeout=` anywhere in either
   file). Fixed: both now accept and pass through `timeout_seconds`. **Not fixed, and correctly
   flagged as a separate, deeper issue**: `main.py`'s `chat()` handler runs `agent.invoke()` via
   `asyncio.to_thread`, and cancelling the outer `asyncio.wait_for` on timeout does not stop an
   already-running thread — it keeps executing after the client sees a timeout response. This
   predates this milestone and needs its own design work.
5. **Shell injection**: `azure-deploy.yml` interpolated `${{ inputs.* }}` directly into `run:`
   script bodies. GitHub Actions performs plain text substitution of `${{ }}` before bash ever
   sees the script, so an input containing a command substitution would execute with this
   workflow's Azure permissions — confirmed live with a harmless probe input containing `$(...)`.
   Practical exploitability today is bounded (only someone who can already dispatch this workflow
   could exploit it, and that person can already deploy arbitrary images into this resource group
   anyway), but it is a well-documented anti-pattern and cheap to fix. Fixed: every input now goes
   through a job-level `env:` var and is referenced as a shell variable, never as a raw `${{ }}`
   inside a script.
6. **The web and API Container Apps shared one managed identity**, which held `Cognitive
   Services OpenAI User` — the web app's nginx never calls Azure OpenAI, so a code-execution bug
   in the (public-facing) web container could have used that identity to call the model directly,
   bypassing planning, verification, and any future rate limiting. Fixed: `platform.bicep` now
   provisions a second identity (`AcrPull` only) for the web app; verified live afterward that the
   web identity holds only `AcrPull`, scoped to the registry.
7. **The `AppRequests`-empty gap (open since the original M3 deployment) had a real, findable
   root cause**, not just an unexplained gap: `FastAPIInstrumentor.instrument_app()` was called
   from inside the `lifespan` context manager. Independently reproduced in an isolated subprocess
   (`TestClient` + `InMemorySpanExporter`, no network): instrumenting inside `lifespan` exported
   zero spans for a real request; instrumenting right after `FastAPI()` construction correctly
   exported the expected `SERVER` span. Fixed: `configure_telemetry()` now runs at module level in
   `main.py`, immediately after `app = FastAPI(...)`, not inside `lifespan`.
8. **nginx's default `proxy_read_timeout` (60s) was shorter than the backend's own
   `request_timeout_seconds` (180s)** — confirmed by inspection (no `proxy_*_timeout` directives
   existed at all). A legitimate 70–170s model/vision call would get nginx's own 504 while the
   backend was still working. Fixed: set to 180s, matching the backend's default.
9. **Audit/evidence is written only to the container's local filesystem**, with no volume or
   external store in `apps.bicep` — confirmed by inspection. A new Container Apps revision (any
   redeploy) loses the entire audit/evidence history, contradicting the local-dev framing in
   `PROJECT_STATE.md` ("the audit trail... persists to a local JSONL file... so audit history
   survives a restart") once the "restart" in question is a revision replacement, not a process
   restart on a persistent disk. **Not fixed in this pass** — needs real persistence (Phase 2,
   OD-20's already-planned PostgreSQL direction), not a workaround; tracked in
   `docs/decisions/REVIEW_REQUIRED.md`.

The review also confirmed, matching what this repository already believed to be true, that no
API key/secret was hardcoded anywhere in the reviewed diff, that ACR's admin user was correctly
disabled, and that the reviewed PRs never touched `graph.py`'s planning/verification code — the
non-strict-JSON-schema decision (D-012, above) does not mean the LLM bypasses deterministic
computation; `QueryPlan`'s own Pydantic validation and the existing capability gate are unchanged.

## D-013 — Vite security upgrade targets the minimum patched major, not npm's suggested latest

GitHub Dependabot flagged four alerts against `apps/web`'s `vite@5.4.21` (and its bundled
`esbuild@0.21.5`): one high (`server.fs.deny` bypass on Windows alternate paths, GHSA `vite`
advisory, patched `6.4.2`), two moderate (`.map`-handling path traversal, patched `6.4.2`; a
`launch-editor` NTLMv2 hash-disclosure issue, patched `6.4.3`), and one moderate (`esbuild`'s dev
server accepting arbitrary-origin requests, patched `0.25.0`). All four affect the Vite/esbuild
dev server only; the deployed app is served as a static build behind nginx (D-012), so none of
these were live-exploitable in production, but an open, tool-reported alert is still worth
clearing.

`npm audit fix --force` proposes `vite@8.2.2` (npm's own "latest," a 5→8 jump across three major
versions, flagged by npm itself as breaking). Checked against the actual GitHub advisories
(`gh api .../dependabot/alerts`), the real fix only requires `vite>=6.4.3` — one major version,
not three. `@vitejs/plugin-react@^4.3.4`'s existing peer range already covers `vite@^6.0.0`
unmodified; only `@vitejs/plugin-react@^8.0.0`-compatible releases (`6.x` of the plugin itself)
force a separate, unrelated architecture change (a `rolldown`/`oxc`-based rewrite requiring new
peer packages). Upgrading to `vite@^6.4.3` + `@vitejs/plugin-react@^4.7.0` (the last `4.x`
release, still on the pre-rolldown architecture) clears all four alerts (`npm audit` → 0
vulnerabilities) with a materially smaller blast radius than the tool-suggested default, and
Vite 6's own migration guide has no breaking change applicable to this repo's minimal
`vite.config.ts` (no `worker.plugins`, no legacy Sass API usage, no CJS `require('vite')`, no SSR
build).

`three`/`@types/three` are unaffected — verified directly (`npm ls three @types/three` still
resolves both to the `0.149.0` D-006 pin after the upgrade, and `npm ci`/`npm install` both
succeed with no `ERESOLVE` conflict). No owner decision was needed: this is a routine dependency
security fix with no product-capability or scope question, unlike the OD-numbered decisions
elsewhere in this log.

## D-014 — Conversation/audit persistence via Postgres, with an explicit boundary on what it does not fix

SPEC-M4 (`docs/specs/SPEC-M4-postgres-state-persistence-v1.md`) closes D-012 Finding 9: audit
history and per-thread conversation context were process-local (a bare dict at
`app.state.conversations`, a local-filesystem JSONL file behind `AuditStore`), so a Container App
revision replacement silently lost both. `ConversationStore`/`AuditStore` (`apps/api/app/
persistence/`) are now explicit interfaces with two implementations each, mirroring D-007's
provider-factory seam exactly: `InMemoryConversationStore`/`JsonlAuditStore` (today's behaviour,
unchanged, the default when `DATABASE_URL` is unset) and `PostgresConversationStore`/
`PostgresAuditStore` (opt-in, Azure Database for PostgreSQL). `ServiceContainer` gained
`conversation_store_factory`/`audit_store_factory` injection parameters (`persistence/factory.py`
centralizes the selection logic, the same way `providers/factory.py` does for `llm_provider`).

**Sync driver, not async, by design (found while implementing).** `AuditStore.append()` is called
throughout `AgentService._audit()` (`apps/api/app/agent/graph.py`), which runs synchronously
inside the worker thread `main.py`'s `chat()` hands work to via `asyncio.to_thread(agent.invoke,
...)` — there is no event loop in that thread for an async-only driver (`asyncpg`) to run on.
`psycopg` (v3, sync) is used instead, callable from exactly the context `AuditStore` has always
been called from. Postgres access in Azure authenticates with an Entra ID token via
`DefaultAzureCredential` (`DATABASE_USE_MANAGED_IDENTITY`, OD-26) — the Flexible Server
(`infra/bicep/data.bicep`) has `passwordAuth: 'Disabled'` outright, continuing OD-23's
zero-stored-secret posture as a platform guarantee, not an application convention, onto this
project's second Azure-managed data resource.

**What this milestone explicitly does not fix.** `app.state.requests` (`main.py`) stores a live
`asyncio.Task` reference per in-flight request, used by `POST /api/v1/requests/{id}/cancel` to
call `task.cancel()` directly on the in-process coroutine. This has no serializable representation
outside the event loop that created it and is unchanged by this milestone — durable conversations
and audit history do not, by themselves, make it safe to lift OD-22's `minReplicas: maxReplicas: 1`
pin (`infra/bicep/apps.bicep`'s inline comment says so directly). Cross-replica cancellation would
need a different mechanism entirely (e.g. a polling-based cancel flag each replica checks, instead
of one replica reaching into another's event loop) and is not scheduled.

**Two real bugs found only by running this against a live Postgres** (`docker compose up
postgres`), neither reachable by a mock: `PostgresAuditStore.by_trace` failed `AuditEvent`
validation because `psycopg` deserializes a `UUID` column into a native `uuid.UUID` object, and
`AuditEvent.id` is a plain `str` field (pydantic accepts a native `datetime` for the timestamp
column without complaint, which is why this wasn't obvious from the schema alone) — fixed by
casting to `str` before validation. And `_TokenRefreshingPool` built its Managed-Identity
connection string as `f"{conninfo} password={token}"`, string-concatenated onto a `postgresql://`
URI — which does not produce valid `libpq` conninfo, since a URI and a keyword/value fragment
don't combine by pasting; no test caught this because the `TEST_DATABASE_URL`-gated suite only
exercised the non-Managed-Identity path. Fixed with `psycopg.conninfo.make_conninfo`, which merges
either conninfo form correctly, extracted into a pure `_conninfo_with_password` function so it is
now covered by a test that needs no real connection.

**`checkpoint_db_path` removed, not repurposed.** `docs/decisions/REVIEW_REQUIRED.md` had flagged
this dead LangGraph-checkpointer setting (found during SPEC-M3's tech-debt scan) as needing a
future milestone's decision: remove it, or decide this is where real persistence belongs. It is a
different mechanism from `ConversationStore`/`AuditStore` (a LangGraph checkpoint vs. this
project's own bespoke stores) — reusing the name for an unrelated thing would be more confusing
than deleting four lines, so it, and `CHECKPOINT_DB_PATH` in `.env.example`, are gone.

**Verification.** `tests/test_postgres_persistence.py::test_a_second_independent_store_instance_
reads_back_what_the_first_wrote` reproduces D-012 Finding 9 directly: two independently
constructed store instances — no shared Python object — stand in for two container processes; the
second reads back exactly what the first wrote, run against a real local Postgres (`TEST_DATABASE_URL`,
skipped by default so CI's no-network-egress policy is unaffected). 186 tests pass; `ruff check
--select F,E9,I,F401` is clean. `infra/bicep/data.bicep`/`apps.bicep`/`platform.bicep` all validated
with `az bicep build`; not run against a real Azure subscription in this same session, since a
Postgres Flexible Server bills continuously once created (unlike Container Apps) and that
required the owner's explicit cost go-ahead first.

**Now deployed and live-verified (2026-09-09, same day, owner-authorized): see
`docs/reports/2026-09-09-m4-azure-deployment-baseline.md`.** The line above is preserved rather
than deleted because it was an accurate statement of that moment, not a mistake — the owner
first confirmed this subscription's free-tier eligibility (`quotaId: FreeTrial_2014-09-01`,
confirmed via the Portal's Free Services page), then authorized the real deployment. That run
found two more real defects unreachable from local `docker-compose` Postgres (this subscription
cannot provision Postgres Flexible Server in `eastus2`; `pgaadauth_create_principal_with_oid`
only exists when connected to the `postgres` maintenance database, not the application database
— the migration script split into `apps/api/migrations/0002a_create_api_runtime_role.sql`/`0002b_grant_api_runtime_role.sql` accordingly),
and produced direct, queryable proof of real conversation rows and 77 real audit events written
by the actually-deployed Container App, not asserted from local tests alone.

**Correction, found the same day by an independent review (below): "186 tests pass" above was
true only against this session's own local `.venv`, which predated `apps/api/migrations/`. GitHub
CI on this branch was actually red on every push up to and including the commit that added the
paragraph above — the claim of a clean, verified state was wrong at the moment it was written,
not something that regressed afterward. Corrected in place, per this repository's own discipline
against unflagged release-claim changes (D-012 set the precedent); see the independent-review
addendum immediately below for the full account.**

### Independent review, 2026-09-09 (same day, second pass)

A second independent review (GPT) audited the pushed branch's actual remote state — not the
working tree — and, unlike the first-round M3 review, this one caught something the M3 process
didn't have a mechanism to catch: **GitHub's own CI for this branch was red**, contradicting the
"186 tests pass" claim above, which had only ever been checked against a local `.venv` created
before `apps/api/migrations/` existed. Every claim below was independently re-verified before
being acted on (this repository's standing rule), not taken on the reviewer's word:

1. **Critical, confirmed via a genuinely clean `python3 -m venv` + `pip install -e apps/api[dev]`,
   not just by reading the CI log**: adding `apps/api/migrations/` gave setuptools two flat-layout
   top-level package candidates (`app`, `migrations`), which it refuses to build rather than
   guess — `pip install` failed outright on every Python version. Fixed with an explicit
   `[tool.setuptools.packages.find] include = ["app*"]`.
2. **Confirmed by fault injection** (fake `ConversationStore`s that raise or stall on command,
   `tests/test_chat_persistence_failure_handling.py`, each test independently verified to fail
   against the pre-fix `main.py` and pass after): `chat()`'s initial `conversations.get()` call
   ran synchronously and outside the endpoint's own error handling — an unreachable Postgres there
   produced an unhandled exception instead of this system's normal `error` disposition
   (D-004/D-010), and every store call ran directly on the event loop, which with a real Postgres
   configured blocks every other concurrent request on this OD-22 single-replica deployment for
   the duration of each DB round trip. A separate bug, also confirmed live-broken and fixed in the
   same pass: `record.update(status="completed", ...)` ran before the final context-persistence
   attempt, so a write failure there could leave `app.state.requests` reporting `"completed"`
   while the actual response was failing.
3. **Confirmed by design review, not live-tested (no Azure Postgres deployed)**:
   `infra/bicep/data.bicep` made the API's own runtime managed identity the Flexible Server's AAD
   administrator — full database-admin privilege for a container whose actual job is two
   INSERT/SELECT/UPDATE statements, meaning a compromised API process could tamper with or delete
   audit records outright. Fixed by separating "who administers the server" (the CI/CD deploy
   principal, which already holds elevated resource-group permissions) from "what the running app
   can do" (a new, unverified-against-live-Azure `apps/api/migrations/0002a_create_api_runtime_role.sql`/`0002b_grant_api_runtime_role.sql`
   granting a plain, non-admin role exactly `SELECT/INSERT/UPDATE` on `conversations` and
   `SELECT/INSERT` — no `UPDATE`, no `DELETE` — on `audit_events`).
4. **Confirmed by code inspection**: `azure-deploy.yml`'s `database_url` input defaulted to
   blank, so a routine redeploy that omitted it would silently revert a persistence-enabled API
   back to in-memory/JSONL mode with no warning. Fixed with a pre-flight check that refuses to
   proceed unless a new `allow_disabling_persistence` input explicitly confirms the downgrade.
5. **P2, confirmed via Microsoft's own documentation for the exact firewall rule name used**: the
   `0.0.0.0`-`0.0.0.0` rule's comment overstated it as scoped to this project's own resources; it
   actually permits any Azure resource in any subscription. Corrected in place, not silently
   edited. Also fixed: `docker-compose.yml`'s local Postgres published on `0.0.0.0` instead of
   `127.0.0.1`; `_TokenRefreshingPool` closed its old connection pool before confirming the
   replacement worked (a token-refresh failure destroyed a still-good pool); no
   connect/pool-wait/statement timeout existed anywhere, so a stalled database could block a
   caller (and, per finding 2, a shared worker thread) indefinitely; the persistence smoke test in
   `azure-deploy.yml` only asserted `disposition != "error"`, which a response that never actually
   inherited context could still satisfy.
6. **A second, distinct CI break, found only because CI was actually re-run after the fixes
   above**, not caught by this session's own local testing (no Python 3.9/3.10/3.11 interpreter
   available on this machine): two `str | None` annotations added to `main.py` during fix #2 above
   crashed on Python 3.9 (`TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`),
   which needs `from __future__ import annotations` for that syntax — already present in
   `config.py`/`postgres_store.py` for the same reason, just missing from this one file. Diagnosed
   directly from CI's own failure output (isolating it to exactly two lines and confirming the
   other three Python versions passed unaffected), not guessed at.

**What was not changed**: `pgaadauth_create_principal_with_oid`'s exact argument order/name
(finding 3's migration script, then a single file) could not be verified against a live Azure
Postgres Flexible Server this session (that extension function does not exist on the local
docker-compose Postgres everything else here was verified against, and no Flexible Server was
deployed) — flagged
explicitly in the script itself rather than presented as verified. Also not addressed: whether
adding a GitHub Actions Postgres *service container* to `ci.yml` (to run the `TEST_DATABASE_URL`-
gated tests in CI, not just locally) is compatible with this repository's own stated CI policy
("no network egress beyond package registries") — pulling `postgres:16-alpine` from Docker Hub is
arguably outside that literal scope even though the container itself then runs with no further
network access. Not decided here; needs an explicit owner call, not a unilateral interpretation —
tracked in `docs/decisions/REVIEW_REQUIRED.md`.

**Both since resolved, same day.** The owner decided the CI service-container question (see
`docs/decisions/REVIEW_REQUIRED.md`'s resolution) — `ci.yml`'s `backend` job now runs a real
`postgres:16-alpine` service container on every push. And the `pgaadauth_create_principal_with_oid`
question was resolved by actually deploying: the function exists exactly as documented, but only
when connected to the `postgres` database, not `armie` — confirmed live, not guessed at, and fixed
by splitting the migration into `0002a_create_api_runtime_role.sql` (runs against `postgres`) and
`0002b_grant_api_runtime_role.sql` (runs against `armie`). Full account:
`docs/reports/2026-09-09-m4-azure-deployment-baseline.md`.

**Verification after all fixes above**: a second, real GitHub Actions run
(`https://github.com/tosspro23-cell/armie-construction-intelligence-workbench/actions`, branch
`feat/m4-postgres-persistence`) passed on all five jobs (`lint`, `backend` × Python 3.9-3.12,
`frontend`) — checked directly via `gh run view --json jobs`, not inferred from a local run. 189
tests pass locally (186 + 3 new fault-injection tests, `tests/test_chat_persistence_failure_handling.py`).
The Managed-Identity connection-string bug from the first pass (already fixed and tested before
this second review) and the two live-Postgres bugs above were all re-verified against a real local
Postgres (`docker compose up postgres`) after these additional changes, confirming no regression.

## D-015 — Shared-secret authentication for the public API

SPEC-M5 (`docs/specs/SPEC-M5-api-shared-secret-auth-v1.md`) closes D-012 Finding 1: any internet
caller could reach `armiem3-web`'s public FQDN and call `/api/v1/chat`, consuming Azure OpenAI
quota with no owner check. Verified live before writing the spec, not assumed: `az containerapp
auth show` returned `{}` for both `armiem3-web` and `armiem3-api` (Container Apps' built-in Easy
Auth is not enabled), `az containerapp ingress access-restriction list` returned `[]` (no IP
restriction), and no route in `apps/api/app/main.py` declared any auth dependency.

**Shared secret, not Entra ID login or IP restriction (OD-28, owner-explicit).** This is a
demo/portfolio reference implementation with no real multi-tenant requirement; a shared secret
closes the anonymous-access gap proportionally, without building a login system this project has
no other use for. `app/security.py`'s `require_api_key` is registered once,
`FastAPI(dependencies=[Depends(require_api_key)])`, so every route requires it uniformly —
including any added later, with no per-route boilerplate or allowlist of "exempt" routes to
maintain. A no-op when `api_shared_secret` is unset, the same opt-in-when-unset pattern as
`database_url`/`otel_exporter_connection_string` (local development stays exactly as open as it
already is). Uses `secrets.compare_digest`, not `==`, for the header comparison — a naive string
comparison leaks how many leading characters of a guess were correct through response-time
differences.

**Explicit, honest limitation, not silently assumed away (D-004 discipline).** A single shared
secret authenticates *possession of the secret*, not *identity*: anyone holding it can still query
or cancel any other holder's requests on `/api/v1/requests/*`. This spec closes "random internet
strangers can't get in at all"; it does not add per-caller ownership checks. Acceptable for this
project's actual usage pattern (the owner, and anyone they hand the key to for a demo), not a
gap this milestone claims to have fixed.

**Found while writing the spec, not assumed:** two frontend endpoints (`/api/v1/project/pdf/pages/
{page}.png`, `/api/v1/evidence/{filename}`) are loaded via raw `<img src=...>` tags, which cannot
carry a custom `Authorization` header. Fixed with `apps/web/src/apiClient.tsx`'s `AuthedImage`
component (`fetch` with the header → blob → `URL.createObjectURL`, revoked on cleanup) instead of
the simpler-looking alternative of a query-string key, which would leak the secret into browser
history and server access logs. This helper module is separate from `main.tsx` (not defined
inline there) specifically because `IfcViewer.tsx` also calls the API directly and must not import
from the app's entry module.

**Azure: a Container Apps native secret, not Key Vault (OD-29, this spec's default, flagged for
owner review).** SPEC-M3 deferred Key Vault because Managed Identity eliminated every secret
Phase 1 needed; this is the first secret Managed Identity cannot eliminate (a browser/end user is
not an Azure identity), but a whole Key Vault resource, access policy, and `secretRef` pointing at
it is disproportionate to one shared demo key. `infra/bicep/apps.bicep`'s `apiSharedSecret` param
is `@secure()`, required with no default (unlike `databaseUrl`, blank here would silently mean "no
auth"), stored as a Container Apps `secrets` entry and surfaced via `secretRef`, not a plain env
value. `azure-deploy.yml`'s new `api_shared_secret` input is masked with `::add-mask::` as the
workflow's first step, since `workflow_dispatch` string inputs are not masked automatically the
way repository secrets are.

**Verification.** A `TestClient(app)`-based test (the first in this suite to route through
FastAPI's real HTTP dispatch for `app.main.app` — every other test calls `main.chat()`/`resume()`
directly as a plain coroutine, which does not run dependency injection) proves the dependency
actually rejects a real unauthenticated request; confirmed to fail when the dependency
registration is removed and pass when restored. The frontend gate/blob-fetch mechanism was
verified end-to-end in a real browser (not just `tsc`/`vite build`): wrong key stays gated,
correct key unlocks the app, and the IFC viewer, chat, trace fetch, drawing-page image, and
evidence-crop image all load correctly through the authenticated path. 195 tests pass; `ruff
check --select F,E9,I,F401` is clean; all `.bicep` files validated with `az bicep build`.

**Deployed and live-verified the same day** (`docs/reports/2026-09-09-m5-azure-deployment-baseline.md`):
independently re-checked directly against the live app, not the deploy workflow's own report --
no `Authorization` header returns `401`, a wrong key returns `401`, the real key returns the same
correct answer the M3/M4 baselines already proved. Also found and resolved in this same pass: a
CodeQL finding on storing the secret in browser storage (`js/clear-text-storage-of-sensitive-
data`) — switched `localStorage` to `sessionStorage` and dismissed the resulting alert with a
written justification specific to this shared, non-differentiated secret's security model.
