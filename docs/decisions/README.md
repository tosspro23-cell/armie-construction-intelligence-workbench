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

## D-016 — Per-caller request ownership via a server-issued session token

Fix work against D-015's own explicitly-flagged limitation (`REVIEW_REQUIRED.md`'s "PARTIALLY
RESOLVED by M5" entry), not a new milestone — per this project's established fix-work convention
(chore/dependency and fix-against-an-existing-milestone's-findings work gets a `D-0xx` entry, not
a new `docs/specs/SPEC-<milestone>` document; see D-012's independent-review fixes and PR #12 for
precedent). Branch `fix/request-ownership-session-token`.

**The gap.** D-015 was explicit that shared-secret auth authenticates *possession*, not
*identity*: any holder of `API_SHARED_SECRET` could query or cancel *any other holder's* request
via `GET /api/v1/requests/{id}` / `POST /api/v1/requests/{id}/cancel` — `app.state.requests`
carried no notion of who created a given record.

**Owner-chosen mechanism (server-issued session token, not Entra ID or client-self-declared ID).**
OD-28 already decided against per-user identity (Entra ID) as disproportionate to this project's
demo/portfolio usage; reopening that trade-off just to fix this one gap would have been a much
larger change than the gap warrants. A client-self-declared caller ID (e.g. a client-generated
`X-Caller-Id`) was rejected too: it is not actually a fix, since a caller can claim any ID,
including another caller's. The middle ground: `POST /api/v1/session` (behind `require_api_key`
like every route) issues a `secrets.token_urlsafe(32)` value the *server* generates; the frontend
fetches one once per gate-unlock (`apps/web/src/apiClient.tsx`'s `ensureSessionId`, called from
`main.tsx`'s `loadMetadata` success path) and threads it back as `X-Session-Id` on every
subsequent request. `app.state.requests[id]["session_id"]` is stamped from the creating request's
`X-Session-Id` at `POST /api/v1/chat` time; `check_request_ownership` (`app/security.py`) is
called by both `GET /api/v1/requests/{id}` and `POST /api/v1/requests/{id}/cancel` before doing
anything else, raising `404` — not `403`, so a non-owner cannot even confirm the request exists —
on a session mismatch.

**Explicit, not a new authentication boundary.** This is caller *correlation*, not identity: the
session token is not itself a secret worth protecting beyond the shared secret that gates issuing
one in the first place, and it carries none of Entra ID's guarantees (no revocation, no per-user
audit identity, no protection if a caller shares their own token). It only stops one holder from
reaching into *another* holder's in-flight requests by ID — the actual, narrow gap D-015 flagged.

**Backward-compatible by design, not by accident.** A request created with no `X-Session-Id`
header (a direct API script that never adopted the session flow) is stamped `session_id: None`,
and `check_request_ownership` treats a record with no owner as unrestricted — exactly pre-D-016
behaviour. This is a deliberate proportionality call, not an oversight: a caller bypassing the
frontend already holds the shared secret and could reach `/api/v1/chat` directly regardless, so
this fix closes the browser-mediated multi-holder case D-015 actually described without requiring
every API integration to adopt a new header just to keep working.

**Verification.** `tests/test_request_ownership.py`: unit tests on `check_request_ownership`
directly, plus one `TestClient(app)`-based end-to-end test (the same real-HTTP-dispatch pattern
D-015 established, since `Header()` resolution — like `Depends()` — does not run when a route
function is called directly as a plain coroutine) proving a session-B caller gets `404` on a
session-A request's status and cancel endpoints, the owning session gets `200` on both, and a
request created without `X-Session-Id` stays open to any caller. Confirmed as a genuine
reproduction, not a tautology: the ownership check was temporarily removed from both routes,
the new end-to-end test failed exactly at the cross-session assertion (`200` where `404` was
expected), then the fix was restored and the full suite re-run green. 201 tests pass (195 + 6
new; 3 Postgres-only cases remain skipped without `TEST_DATABASE_URL`, unaffected by this fix);
`ruff check --select F,E9,I,F401` clean; `npm run build` (`tsc -b && vite build`) clean.

## D-017 — Multi-document corpus and its naive deterministic baseline (SPEC-M6)

`docs/specs/SPEC-M6-multi-document-corpus-v1.md`, revised in place after owner review (its own
Rationale section keeps that revision note — see there for why the first draft's one-document,
one-collision scope was rejected as insufficient). Implements the Phase 3 prerequisite from
`PROJECT_STATE.md`'s 2026-09-10 scoping note: today's system hardcodes exactly one PDF
(`Settings.pdf_file`, one `DocumentAnalyzer`, no document field on `QueryPlan`), so there was no
way to build or evaluate real "enterprise retrieval" against it, and no way to prove semantic
search would beat what already exists.

**Config/plumbing (§A).** `Settings.pdf_files: list[str]` replaces `pdf_file` (singular),
defaulting to a one-element list — no behaviour change for an unmodified deployment.
`ServiceContainer.document_analyzers: dict[str, DocumentAnalyzer]` builds one analyzer per
configured file, in configured order (not filesystem/glob order, which is platform-dependent).
`ServiceContainer.document_analyzer` (singular) becomes a property returning the primary
(first-configured) analyzer — a deliberate scope boundary, not a shim: the raw PDF/page-image
viewer endpoints and the door/window reconciliation pilot (OD-15/D-011) both stay scoped to one
document in this milestone.

**`QueryPlan.requested_document` (§B).** `None` means "search every configured document" —
today's only reachable value. Not wired to any live API/UI override in this milestone
(natural-language document-name inference is explicitly SPEC-M7's concern); it exists so
`_execute_pdf` can honor an explicit target when one is set by a future planner, and so the
single-document path (when exactly one document is configured, or one is explicitly named)
stays byte-for-byte identical to pre-M6 behaviour.

**The naive baseline, and two evidenced failure modes, not one (§C).** `_execute_pdf_multi_document`
runs `native_lookup` against every configured document, zero model calls, and never falls
through to vision (vision resolves "what's on this page," not "which document"). Three outcomes:
exactly one confident hit → answered (citation's `locator` gains a `document` field —
`Evidence.source_file` was already correct per-analyzer, but `Citation`/`_citations` never
surfaced it); more than one confident hit → `clarification_required` naming every candidate
(**precision failure**, OD-32); zero confident hits → `clarification_required` (**recall
failure** — covers both a genuine no-answer case and a vocabulary-mismatch miss the baseline
cannot distinguish from it, OD-33). The owner explicitly rejected precision failure alone as
sufficient justification for Azure AI Search: a keyword collision only proves *some*
disambiguation step is architecturally necessary, which a simpler rule-based router could also
provide. Recall failure — a realistically-phrased question whose answer exists under different
vocabulary than any table uses — is the stronger case, since no amount of additional keyword
rules can fix a vocabulary gap.

**The corpus (§D), ~15-20 documents, 3-4 types, explicitly a mechanism demonstration, not an
enterprise-scale claim (OD-30, owner-confirmed).** 17 new documents in `demo_data/corpus/`
(`armie_demo_schedule.pdf`/`armie_demo.ifc` untouched — SPEC-M2's Tag/reconciliation fixture work
depends on the original schedule staying exactly as it is): 6 schedule variants reusing the
original's exact table geometry (so D-009's `native_lookup` characterization holds without
re-deriving it), 4 door/window spec sheets (different field vocabulary, for genuine diversity), 4
RFI log entries and 3 meeting-minutes excerpts (narrative, not required to satisfy D-009's
tabular characterization at all). "Panel-A" is a real, distinct panel independently reused by two
different wings' schedules (precision-failure fixture, OD-31); `rfi_log_047.pdf` states in prose
that "Panel-E" was sized for a "connected capacity of 15.75 kW," deliberately not the tables'
"Connected Load (kW)" vocabulary, and explicitly not yet in any issued schedule — no table in the
corpus contains Panel-E at all (recall-failure fixture, OD-31). Empirically verified before
writing any test: narrative documents degrade to a graceful 0.0-confidence miss rather than a
crash or a bogus parsed table.

**Verification.** 26 new tests (`tests/test_multi_document_corpus.py`), covering `ServiceContainer`
plumbing, both failure modes, the single-match-among-many case, narrative documents never
producing a false hit, and the full 18-document corpus loading and answering correctly. Confirmed
as genuine reproductions, not tautologies: the multi-document branch was temporarily forced off
(simulating pre-M6 behaviour) and the collision test and full-corpus test both failed exactly
where expected, then the fix was restored and the full suite re-run green. Also manually verified
over real HTTP (`TestClient`, not just `AgentService.invoke` directly) — the collision question,
the recall-failure question, and a genuine single match all produced exactly the responses the
tests assert. 227 tests pass (201 + 26 new), 3 Postgres-only skipped, unaffected; `ruff check
--select F,E9,I,F401` clean; `npm run build` clean. Every existing PDF-lookup test passes
unmodified (mechanical fallout only: `Settings(pdf_file=...)` → `Settings(pdf_files=[...])` at
each call site, no assertion changed).

**Explicitly not done here (SPEC-M7's scope, gated on this milestone's evidence actually
existing).** No Azure AI Search, no vector index, no embeddings, no ADLS. No natural-language
document-name inference. No extraction of any kind from narrative documents — if SPEC-M7's
retrieval step ever *locates* the right passage in one, reading it back out is a separate, later
scope decision.

## D-018 — Azure AI Search retrieval, evaluated against D-017's own two named failure modes

`docs/specs/SPEC-M7-azure-ai-search-retrieval-v1.md`. Builds the capability D-017's naive baseline
exists to justify or refute, evaluated against that milestone's own precision/recall failure
fixtures rather than a fresh scenario. Full live evidence in
`docs/reports/2026-09-10-m7-azure-ai-search-baseline.md`; this entry records the design decisions
and their real, measured justification — per the owner's explicit instruction for this milestone,
conclusions here come from data against the live service, not narrative.

**Real infrastructure, not hypothetical.** `armiem3-search` (Azure AI Search, `sku: free`,
`centralus` — `eastus2` was out of capacity, the same restriction D-014 hit for Postgres) was
created during the cost-feasibility check and kept for direct use (OD-34, owner-confirmed) rather
than deleted and re-decided later. `text-embedding-3-small` deployed on the existing
`armie-m3-openai` resource — no new Azure OpenAI resource. `armiem3-search` has
`disableLocalAuth: true` set (no API-key path exists at all); RBAC is least-privilege and
role-split, continuing OD-23's zero-stored-secret posture onto a third Azure-managed resource: the
API's runtime identity holds only `Search Index Data Reader` (query), while index/schema
management (`Search Index Data Contributor` + `Search Service Contributor`) and the one-time
`Cognitive Services OpenAI User` grant needed to run the indexing script belong to the operator
identity, not the running API.

**Push-based indexing, not ADLS + an indexer/skillset pipeline (§B, OD unchanged from the spec).**
`scripts/index_document_corpus.py` extracts whole-document text (PyMuPDF, no chunking — every
document in this corpus is a single page), embeds it via the new `EmbeddingProvider` seam, and
upserts directly into the index. Standing up Blob Storage plus an indexer for 18 single-page
documents would be disproportionate infrastructure, mirroring OD-29's reasoning for the same
question applied to storage instead of secrets.

**A real defect, found only by running the actual end-to-end path against the live index, not by
the CI-safe unit tests.** The first indexing run stored each document's `filename` as its
`Settings.pdf_files`-relative path (e.g. `"corpus/schedule_l2_east.pdf"`); `ServiceContainer.
document_analyzers` keys its dict by the bare basename. `_retrieve_relevant_documents` filters
retrieved filenames against that dict, so the mismatch silently excluded every retrieval
candidate — a live `TestClient` run returned the unchanged blanket miss message instead of a
directed one. The CI-safe fake-search-client tests could not have caught this: they inject
filenames that already match, by construction. Fixed by storing the basename in the index and
rebuilding it from scratch (cheaper and cleaner than migrating 18 documents' ids in place).

**The asymmetric acceptance bar held, on real data, with an honest caveat found by testing it
rather than assuming it.** Hybrid search (BM25 + vector) ranks `rfi_log_047.pdf` first for
SPEC-M6's exact recall-failure question, end-to-end verified via a live `TestClient` call
(`clarification_required`, naming the RFI first, `model_call_count: 1`); the precision-failure
(Panel-A) case is unchanged, verified by call-count (`search_client.calls == []`,
`model_call_count: 0` on that path), not just by the unchanged answer text. Checked further,
rather than declaring victory on the first result: **on this specific fixture question,
pure-vector-only search does *not* rank the RFI first** (it ranks third; hybrid's win is
substantially attributable to BM25 matching the literal token "Panel-E," which appears verbatim
in the RFI's prose) — a real, useful outcome, but a weaker claim than "embeddings resolved a
vocabulary mismatch." A further, deliberately paraphrased query that avoids the literal entity
name entirely does resolve correctly on pure vector similarity alone, by a wide margin (0.787 vs.
0.708 runner-up) — that is the genuinely semantic version of the claim. Both results are recorded;
neither is allowed to stand in for the other.

**`azure_search_relevance_threshold` = 0.025 (OD-35), set from measured score distributions, not
picked in advance.** Every query tried against the live index showed the same bimodal split:
topically relevant documents scored 0.030–0.033, clearly irrelevant ones scored 0.014–0.019,
consistently across every question tested. 0.025 sits in the observed gap.

**`text-embedding-3-small` (OD-36, as recommended, unrevisited).** The real-index evidence above
did not surface a case where embedding quality was the limiting factor at this corpus's scale, so
there was no basis to revisit the recommended default.

**Verification.** 7 new CI-safe unit tests (`tests/test_azure_ai_search_retrieval.py`), fake
search client + fake embedding provider injected via `ServiceContainer`'s factory seam (D-007
discipline) — the real-index claims above are not re-asserted here, since they can only be tested
against a live Azure AI Search index (this report, not a CI gate, per the spec's own Acceptance
criteria). Confirmed as genuine reproductions, not tautologies: temporarily gutted the retrieval
call sites in `_execute_pdf_multi_document`, confirmed exactly the two directed/blanket-miss tests
failed as expected, restored, reran the full suite green. 234 tests pass (227 + 7 new); `ruff
check --select F,E9,I,F401 apps/api tests` clean; `npm run build` clean (no frontend change was
needed — the new miss message is plain text through an existing render path).

## D-018 addendum — a dedicated Audit Trail card, and a real Azure AI Search Free-tier limitation

Both found during the owner's own hands-on walkthrough of the live system after the milestone
above, not anticipated in the original spec.

**The retrieval evidence was real and correct but hard to find.** The owner had to expand several
unrelated Raw Trace payloads to locate the actual retrieval scores driving a directed-vs-blanket
miss decision. Fixed by making `_execute_pdf_multi_document` (`app/agent/graph.py`) emit a new,
dedicated audit event (`event_type: "retrieval_evaluated"`, added to `AuditEvent`'s `event_type`
Literal) whenever retrieval actually ran and returned any configured-document candidate — payload
is `documents_evaluated` (filename/score pairs), `relevance_threshold`, and `directed_candidates`,
independent of whether any candidate cleared the bar (a below-threshold result is still worth
seeing, not just a silent blanket miss). `apps/web/src/main.tsx` looks for this exact event type
and renders a bordered, left-accented "AI Search Retrieval" card directly under the Decision
Summary — a small table (Document / Relevance score / Named in answer?) — instead of relying on
the generic Raw Trace list. Absence of the event in a trace is itself informative: it means
retrieval wasn't attempted or configured for that turn. 9 tests now cover the seam (2 new:
asserting the event's exact payload when a candidate clears the threshold and when none do, and
its absence when retrieval isn't configured); verified live in a real browser against the live
Azure services, not just unit-tested.

**Azure AI Search's Free tier does not reliably support diagnostic (`OperationLogs`) export.**
Investigated, not assumed, after enabling `armiem3-search`'s diagnostic settings (`OperationLogs`
+ `AllMetrics` -> the existing `armiem3-logs` Log Analytics workspace) and finding `AllMetrics`
flowing normally while `OperationLogs` stayed empty for 30+ minutes -- well past normal
first-time propagation delay. Microsoft's own documentation confirms this is a real, tier-related
limitation, not a misconfiguration: "Conditions that maximize the integrity of data measurement
include: Use a billable service (a service created at either the Basic or a Standard tier). The
free service is shared by multiple subscribers, which introduces a certain amount of volatility
as loads shift" ([Monitor Queries - Azure AI Search](https://learn.microsoft.com/en-us/azure/search/search-monitor-queries)).
**Owner decision:** stay on the free tier; do not pursue Azure-side per-query diagnostic logs for
this milestone. The app's own `AuditStore` (JSONL/Postgres) is the verified, reliable source of
retrieval evidence regardless of this limitation -- confirmed unaffected, since it never depended
on Azure Monitor. Upgrading to Basic (~$0.101/hour, a real recurring cost) to get reliable
`OperationLogs` remains an option, not exercised here. The diagnostic setting itself
(`armiem3-search-diagnostics`) is left enabled -- `AllMetrics` is genuinely useful and free;
`OperationLogs` may eventually start flowing (or not) with no further action required either way.

## D-019 — Per-caller rate limiting on `/api/v1/chat`

Fix work against D-012 Finding 1's remaining half, not a new milestone -- D-016 (per-caller
request ownership) explicitly left this open ("rate limiting was also not bundled in, the
owner's explicit choice"), matching this project's chore/fix-vs-feat convention (D-012's
independent-review fixes, PR #12 precedent). Branch `fix/per-caller-rate-limiting`.

**Scope: `/api/v1/chat` and `/api/v1/chat/{id}/resume` only, not app-wide like `require_api_key`.**
Rate limiting exists to bound Azure OpenAI/Search quota spend; every other route (`/api/v1/health`,
`/api/v1/project/*`, `/api/v1/requests/*`) either costs nothing or is already covered by D-016's
per-caller ownership check. A route-level `dependencies=[Depends(require_rate_limit)]` on just
these two routes, not a global one, keeps that distinction explicit rather than rate-limiting
routes that were never the actual concern.

**`InMemorySlidingWindowRateLimiter` (`app/rate_limit.py`), in-process only -- a deliberate,
documented proportionality choice, not an oversight.** This project pins `minReplicas:
maxReplicas: 1` (OD-22) precisely because `app.state.requests` has no cross-replica
representation; a rate limiter sharing that same constraint costs nothing further and avoids
introducing Redis or any other shared-state dependency for a demo-scale, single-replica
deployment. If OD-22's pin is ever lifted, this limiter would need replacing with a shared
store at the same time, not before -- the docstring says so explicitly.

**Keyed by `X-Session-Id` when present, falling back to the raw `Authorization` header value
otherwise (OD unchanged, mirrors D-016's own compatibility choice for the same case).** Each
browser tab gets its own budget once it has adopted the session flow; a direct API caller with
no session shares one bucket with every other unidentified caller -- proportional, not a new gap,
for the same reason D-016 left an unowned request unrestricted rather than inventing identity
where none exists.

**`rate_limit_requests_per_minute` unset by default (opt-in-when-unset, the same pattern as
`api_shared_secret`/`database_url`/`azure_search_endpoint`).** No developer or CI environment is
required to configure a limit just to run this project; local development and any deployment
that omits it stays exactly as unlimited as it is today.

**Verification.** 5 new tests (`tests/test_rate_limit.py`): the limiter's own allow/reject/window-
slide behaviour with an injectable fake clock (no sleeping); a real `TestClient(app)` end-to-end
test proving a second request from the same session within the window returns `429` with a
`Retry-After` header while a *different* session's first request still succeeds (independent
budgets, not a global counter); and an explicit no-op check when unset. Confirmed as a genuine
reproduction, not a tautology: the route-level dependency was temporarily removed, the new
end-to-end test failed exactly at the `429` assertion (got `200` instead), then the fix was
restored and the full suite re-run green. 241 tests pass (236 + 5 new); `ruff check --select
F,E9,I,F401 apps/api tests` clean; `npm run build` clean (no frontend change needed -- a `429`
surfaces through the existing generic error-handling path).

## D-020 — Evidence crop persistence via Azure Blob Storage

`docs/specs/SPEC-M8-evidence-blob-persistence-v1.md`. Closes the one piece SPEC-M4/D-014 (OD-24)
explicitly deferred: cited PDF evidence crops (`Evidence.locator["evidence_crop"]`) lived only on
local Container App disk, so a revision replacement broke old citations even though the database
describing them in full (D-014's Postgres-backed `AuditStore`/`ConversationStore`) survived fine.
Full live evidence in `docs/reports/2026-09-11-m8-evidence-blob-persistence-baseline.md`.

**Scoped narrower than "persist everything `evidence_dir` writes," verified before scoping, not
assumed.** Grep-verified every call site of `DocumentAnalyzer.render_page` and `crop_evidence`:
`render_page`'s output is never stored by filename anywhere -- every caller either serves it once
immediately or feeds it straight into a vision-model call as base64 and discards the path. Only
`crop_evidence`'s output filename is ever embedded into a persisted `locator`. This milestone
accordingly touches only `crop_evidence` and the `/api/v1/evidence/{filename}` serving endpoint.

**Blob Storage, not literally "ADLS Gen2" (OD-37, this spec's default, flagged for owner
review).** Prior documents (`PROJECT_STATE.md`'s Phase 3 scoping note, D-018's addendum)
informally named this gap "needs ADLS" -- verified before building anything, that framing was
never a deliberate decision: this project's actual need is opaque-filename PNG lookup, no folder
hierarchy, no analytics workload, none of which benefit from ADLS Gen2's defining feature (a
hierarchical namespace). Plain Blob Storage (Standard LRS, one flat container) is the proportional
choice, mirroring OD-29's "disproportionate infrastructure for [a narrow, small need]" reasoning
already established for this project's Key Vault (D-015) and ADLS/indexer-pipeline (D-018)
decisions.

**Dual-write, not a replacement (§B).** `crop_evidence` keeps writing the local file exactly as
before -- no call site or return type changes -- and, when a container-client factory resolves to
a real client, additionally uploads the same PNG bytes under the same filename. A factory, not a
stored instance (mirrors `app/retrieval.py`'s `search_client_factory`, the same per-call-
construction trade-off already accepted for Azure SDK clients in this project). A transient
upload failure is logged (`logger.warning`, since `DocumentAnalyzer` has no audit-trail access of
its own) but never fails the request -- losing durability for one crop is a strictly lesser
failure than losing an already-computed, already-verified answer, the same proportionality
D-014's `context_persist_error` handling already established for conversation-context writes.

**`GET /api/v1/evidence/{filename}`: blob-first, local-fallback (§C).** Reads
`ServiceContainer.blob_container_client_factory` (stored on the container, not only bound into
each `DocumentAnalyzer`, so tests -- and this endpoint -- can substitute a fake the same way they
already do for `search_client_factory`, never by monkeypatching `app.evidence_storage`'s
production global). Tries blob storage first when configured (the durable source of truth once
enabled); falls back to the local file if blob storage doesn't have it (covers evidence written
before this was enabled, or a crop whose upload transiently failed); 404s only if neither has it.

**Verification.** 6 new tests (`tests/test_evidence_blob_persistence.py`): the opt-in factory's
None-when-unset behaviour; `crop_evidence`'s dual-write (asserted against real PNG magic bytes,
not a stub); a failed upload not failing the request; and the endpoint's blob-first/local-fallback
behaviour from both directions. Confirmed as a genuine reproduction, not a tautology: temporarily
removed the upload call, confirmed the dual-write test failed exactly where expected, restored,
reran the full suite green. **Independently confirmed live, not only in CI-safe fakes** (this
project's own precedent: a persistence claim needs proof against the real service) -- see the
baseline report for the real Storage Account, real upload, and the actual claim (local file
deleted, endpoint still serves the crop from blob storage) demonstrated end-to-end. 247 tests
pass (241 + 6 new); `ruff check --select F,E9,I,F401 apps/api tests` clean; `npm run build` clean;
`infra/bicep/*.bicep` (including the new `evidence.bicep`) validate with `az bicep build`.

**Update, 2026-09-11: deployed and live-verified.** Owner-authorized redeploy of `armiem3-api`/
`armiem3-web` with `evidence_storage_account_url` set to the real `armiem3evidence` blob endpoint
(the same `workflow_dispatch` run that also carried D-021 below). Verified against the live app,
not just the workflow's own smoke tests: asked a real question ("What is the connected load for
Panel-A?") through the deployed API, got back a citation naming a freshly-generated evidence crop,
confirmed with `az storage blob list` that the exact file (matching size, fresh timestamp) exists
in the real `evidence` container, then fetched it back through the live `GET /api/v1/evidence/
{filename}` endpoint and confirmed the returned bytes are the same real PNG. No open item remains
on this milestone.

## D-021 — `API_SHARED_SECRET` moved from a `workflow_dispatch` input to a repository secret

Found while reviewing `azure-deploy.yml`'s CI history for D-020's deployment: the 2026-09-09
run's own log (`gh run view <id> --log`) shows `API_SHARED_SECRET` in cleartext once, in the
job-level environment annotation GitHub prints ahead of that run's very first step -- the same
step (`Mask the API shared secret in this run's logs`, `echo "::add-mask::$API_SHARED_SECRET"`)
that was supposed to prevent exactly this. Every later step's own annotation shows the value
correctly masked (`***`), confirming the mask directive worked from the moment it ran -- the gap
is specifically that GitHub renders a step's environment annotation before that step's commands
execute, so the one step whose job was to mask the value could not mask its own annotation.
`workflow_dispatch` inputs have no "secret" type (only `string`/`boolean`/`choice`/`environment`/
`number`), so a value threaded in as one is, by construction, an ordinary string GitHub has no
prior reason to treat specially -- `::add-mask::` is a request to mask output *from that point
forward*, not a retroactive or preemptive guarantee.

**Fix, not a workaround**: `api_shared_secret` is no longer a workflow input at all.
`azure-deploy.yml` now reads `${{ secrets.API_SHARED_SECRET }}` directly -- a genuine GitHub
Actions secret, which the runner masks from before the job starts, in every log line including
that same early environment annotation, per GitHub's own documentation on secret redaction. The
manual `::add-mask::` step is removed as no longer necessary (and, on its own, not sufficient --
leaving it would misstate where the guarantee actually comes from). Because `required: true` on
an input has no equivalent for a repository secret (an unset one silently resolves to an empty
string), a new pre-flight step fails the run closed if `API_SHARED_SECRET` is empty, rather than
deploying Container Apps with no auth enforced -- mirroring the existing "verify the model
deployments exist" and "refuse to silently disable persistence" guards' fail-closed shape.

**Remediation, not just prevention**: the leaked value was rotated (a fresh secret generated and
set as the `API_SHARED_SECRET` repository secret; the previous value stops working the moment the
next deploy runs, since the Container App's own `API_SHARED_SECRET` env value is replaced too).
The historical leak lived only in that one run's own log, visible to anyone with read access to
this already-public repository's Actions history -- not a secret meant to gate anything beyond
this demo's shared-secret auth (D-015, OD-28) in the first place, but rotated on the same
fail-closed principle as any exposed credential regardless of its blast radius.

**Verification note, stated honestly rather than overclaimed**: GitHub's secret-masking guarantee
is a platform behavior, not something this repository's own test suite can fault-inject or
independently reproduce the way D-020's blob dual-write was verified. What was checked directly:
`grep` confirms no remaining reference to `inputs.api_shared_secret` anywhere in the workflow;
the edited YAML parses (`yaml.safe_load`); and the new pre-flight step was read against GitHub's
documented behavior for empty `secrets.*` values (silently `''`, never a workflow parse error).
**Update, 2026-09-11: confirmed live**, in the same `workflow_dispatch` run that deployed D-020.
`gh run view <id> --log` for that run shows `API_SHARED_SECRET: ***` in every step's environment
annotation, including the very first one (`Run actions/checkout@v4`) -- earlier than even the
first step of the previous version's own run, and unlike that run, with no cleartext occurrence
anywhere in the log. Also confirmed end-to-end, not just in the log: the leaked value now returns
`401` from the live API, and the newly-rotated secret (generated and set as the `API_SHARED_SECRET`
repository secret in the same pass) returns `200`.

## D-022 — Azure AI Search (SPEC-M7) was never actually wired into `azure-deploy.yml`

Found while auditing what is genuinely live in production versus merged-but-inert, prompted by
the same M8 deployment gap this session had just closed. SPEC-M7's own "Affected surfaces"
section named `infra/bicep/` (embedding deployment, `armiem3-search`'s RBAC role assignment) as
in scope, but the milestone shipped without it: the real `armiem3-search` service and its RBAC
were provisioned by hand (`az role assignment create`, `az search index create`) during that
milestone's own live baseline session, never codified in `platform.bicep`/`apps.bicep`, and
`AZURE_SEARCH_ENDPOINT` was never one of `azure-deploy.yml`'s inputs. Confirmed directly against
the live app, not inferred from the workflow file alone: `az containerapp show`'s env list for
`armiem3-api` had no `AZURE_SEARCH_ENDPOINT` entry at all. The practical effect: every real
deployment since M7 merged has been running SPEC-M6's naive keyword baseline for multi-document
lookup, not M7's hybrid retrieval -- despite `PROJECT_STATE.md` correctly saying M7 was "already
deployed/live-evidenced," which was true of the *baseline session's* ad hoc setup, not of the
repeatable deploy pipeline this project otherwise treats as the source of truth for what is live.

**Fix.** `azureSearchServiceName` (blank by default) is a new `platform.bicep` parameter that
computes the deterministic `https://<name>.search.windows.net` endpoint (Azure AI Search has no
custom-domain property to look up the way Cognitive Services accounts do, so no `existing`
resource reference is needed for this alone). `apps.bicep` threads that endpoint into
`AZURE_SEARCH_ENDPOINT` with the same conditional-append pattern as `databaseEnv`/`evidenceEnv`.
`azure-deploy.yml` gains an `azure_search_service_name` input (blank default, same opt-in shape
as `evidence_storage_account_url`) and a new smoke test, gated on that input being set, that asks
SPEC-M6's own recall-failure fixture question ("What is the connected load for Panel-E?") and
requires the *directed* clarification naming `rfi_log_047.pdf` -- not "answered": retrieval only
ever performs document location here, never answer synthesis, so asserting "answered" would
itself be the wrong bar and would have masked exactly this kind of "wired but inert" gap in a
future regression. `azureSearchIndexName`/`azureOpenAiEmbeddingDeployment` are left at
`config.py`'s own defaults, which already match what the real index was built with.

**RBAC deliberately stays out of this template**, found live rather than assumed. A first version
of this fix also added a `platform.bicep` role assignment granting the API identity **Search
Index Data Reader**, mirroring `openAiUserAssignment`'s existing pattern -- and it failed on a
real deployment: `Authorization failed ... does not have permission to perform action
'Microsoft.Authorization/roleAssignments/write'`. Checked directly (`az role assignment list`),
not guessed: the GitHub OIDC deploy identity holds "Role Based Access Control Administrator" with
an ABAC condition restricting `roleAssignments/write` to exactly two role-definition GUIDs
(AcrPull, Cognitive Services OpenAI User) -- itself D-012's deliberate fix for an unconditioned
RBAC-delegation privilege-escalation path on this same identity. Adding a third allowed role here
would have partially reopened exactly what D-012 closed, for the sake of one milestone's
convenience. Corrected fix: RBAC for Search stays a manual, out-of-band step, the same pattern
`data.bicep`/`evidence.bicep` already use for their own role assignments -- and it turns out this
was already done for the real `armiem3-search`/`armiem3-identity` pair, during SPEC-M7's own
baseline session (confirmed via `az role assignment list` against the live scope: `armiem3-
identity` already holds Search Index Data Reader there), so no new manual step was actually
needed to complete this fix.

**Second real finding, same deployment attempt.** With the RBAC correction above, `Deploy
Container Apps` succeeded and `AZURE_SEARCH_ENDPOINT` was genuinely present on the live
`armiem3-api` -- but the new smoke test still failed: the Panel-E question returned
`clarification_required`, correctly, but naming `DB-L1-A`/`DB-L2-B`/`Panel-A` (the single-
document schedule's own records), not `rfi_log_047.pdf`. Root cause, found live rather than
assumed: `armiem3-api` had never been deployed with SPEC-M6's multi-document corpus enabled
either -- `PDF_FILES` was never one of `azure-deploy.yml`'s inputs any more than
`AZURE_SEARCH_ENDPOINT` was, so `_execute_pdf_multi_document` (the retrieval fallback's only
caller) never ran at all; the deployed app has been serving the pre-M6 single-document baseline
this whole time, unrelated to whether Search is configured. This is not a defect in M6 itself --
its own "Affected surfaces" section deliberately excluded this template, "so no deployment that
doesn't override it changes behaviour" (its own `PROJECT_STATE.md` entry) -- it is simply a second
opt-in setting that also needed threading through for the *deployed* app to exercise M7 at all.
Fixed the same way: a `pdfFiles` `apps.bicep` parameter (blank default) and a `pdf_files`
`azure-deploy.yml` input, conditionally appended into env exactly like `databaseEnv`/`evidenceEnv`/
`searchEnv`. Confirmed the JSON-array value survives `az deployment group create --parameters
pdfFiles="$JSON_ARRAY"` unmangled -- checked directly via `az deployment group validate`'s own
`properties.parameters.pdfFiles` output, not assumed, since a value that *looks* like JSON passed
through a CLI `key=value` parameter is a genuine, real risk of misparsing worth checking rather
than guessing correct.

**Verification.** `az bicep build` on both edited templates, clean, no new warnings, after both
corrections above. Deployed for real via `workflow_dispatch` against `armiem3-search`/
`armiem3-api` with both `azure_search_service_name` and `pdf_files` set -- all steps passed,
including the new smoke test. Independently re-verified directly against the live app afterward
(not only the workflow's own smoke test): the exact Panel-E question returned
`clarification_required` naming `rfi_log_047.pdf` first, and its trace's `retrieval_evaluated`
event shows real Azure AI Search hybrid scores against all 18 corpus documents --
`rfi_log_047.pdf` at `0.03280`, matching the original SPEC-M7 baseline report's own measured
score for this exact question -- at `model_call_count: 0`. This is the actual, deployed-app
version of SPEC-M7's central claim; M7 was previously live-evidenced only against the baseline
session's own ad hoc setup, never through the repeatable deploy pipeline until this fix.

## D-023 — Multi-project workspace via Azure Data Lake Storage Gen2 (SPEC-M9)

Owner-chosen next step after M8, over the alternative of AKS/BI expansion (Phase 4-5):
`ServiceContainer` resolved exactly one implicit project (`ifc_file`/`pdf_files` as global
settings) through every milestone up to M8 -- SPEC-M6 made the *document* dimension plural
within that one project; this is the first milestone to make the *project* dimension itself
plural. Real, application-level project switching (OD-38, chosen over an infrastructure-only
proof), one additional project (`westgate`, OD-39) alongside `demo`.

**Why ADLS Gen2, not plain Blob Storage (correcting an early instinct to reuse D-020's
reasoning).** D-020 already corrected the informal "evidence crops need ADLS" framing --
opaque-filename PNG lookup with no hierarchy needs nothing HNS-specific. Multi-project source
storage is different in kind, not degree: each project is a real, named directory tree
(`<project>/ifc/...`, `<project>/pdf/...`) an operator can `ls`, and directory-scoped POSIX ACLs
(`user:<oid>:rwx`, HNS-exclusive) are the access-control mechanism this spec's §G explicitly set
out to prove -- not RBAC role-assignment conditions (ABAC), which work identically on plain Blob
Storage and would not have proven anything ADLS-specific. Confirmed live with a fresh, minimal
service principal (created via `az ad sp create-for-rbac --skip-assignment`, never the operator's
own `az` session) and direct REST calls to the `.dfs.core.windows.net` endpoint, deliberately
bypassing the API's own broader RBAC grant to isolate the ACL mechanism itself: a positive read
against `westgate/` returned `200` with hash-identical content; the identical call against
`demo/` returned `403 AuthorizationPermissionMismatch`; and an audit of the storage account's
role assignments plus the directory's own ACL confirmed the test identity held zero RBAC roles
and an ACL scoped to exactly `westgate/` and its parent traversal path -- so the `200` above is
attributable to the ACL, not some other authorization path RBAC/ACL being OR'd could have masked.

**Design.** `ServiceContainer.get_project(project_id)` resolves a `SourceManifest` from a
committed registry (`demo_data/projects_registry.json`, OD-41: one frozen `source_set_id` per
project -- a content change mints a new one, never edits behind an existing one) to a
`ProjectResources` (its own `ifc_repository`/`document_analyzers`), download-verify-then-
atomically-publish per project on first access, guarded by a per-`project_id` `asyncio.Lock` so
two concurrent first-requests for the same uncached project await one real download rather than
racing two. `GraphState["project_resources"]` threads through `AgentService.invoke()`'s initial
state and every one of `graph.py`'s 9 call sites that previously read `self.container`/
`self.settings` directly (a spec's stated 3-method affected-surfaces estimate turned out
incomplete once in the code -- expanded to the real 9 per this project's own stop-condition
discipline, owner-authorized rather than narrowed). `ConversationStore.bind_project(thread_id,
project_id)` gives one thread exactly one project for its lifetime via atomic claim-or-read
semantics (`dict.setdefault` under a lock in-memory; `INSERT ... ON CONFLICT DO UPDATE SET
thread_id = thread_projects.thread_id RETURNING project_id` in Postgres, a new `thread_projects`
table rather than a column on `conversations`, preserving `resume()`'s None-vs-populated `.get()`
distinction) -- enforced backend-side with a `409 project_mismatch`, not merely a frontend
convention, per the independent review that flagged the first spec draft's OD-40 as
under-specified.

**Independent review before implementation.** A review of the first committed spec draft found
several P1/P2 gaps before any code was written: OD-40 relying on frontend convention alone (no
backend enforcement); `resume()`'s `ChatRequest` reconstruction losing project info entirely;
`IfcViewer.tsx` fetching viewer elements independently of `main.tsx`'s own project state; evidence
UUIDs alone not proving provenance (no committed content hash); the ABAC/ACL conflation above; no
stated concurrency/failure contract; and retrieval's `project_id != "demo"` exclusion looking like
an accident of corpus scale rather than an asserted gate. All revised into the spec before
implementation began, not discovered mid-build.

15 new tests (`tests/test_multi_project_adls.py`: opt-in/no-fallback, download-verify-cache,
concurrency via a real `ThreadPoolExecutor` race, corruption-then-clean-retry, cache-key
scoping by `source_set_id` not `project_id` alone, `bind_project` claim/mismatch semantics, and
three `TestClient`-based end-to-end cases) plus mechanical `project_resources=` fallout across 9
existing test files. 262 tests pass; `ruff` clean; `npm run build` clean.

**First real deployment attempt surfaced three more real defects, none catchable by the CI-safe
suite above -- each following this project's own "verify against the real service" discipline.**

1. **Missing migration and missing grant (live Postgres only).** `thread_projects` existed in
   `apps/api/migrations/0003_thread_projects.sql` but had never been applied against the real
   Azure Database for PostgreSQL -- only against CI's ephemeral instance -- so the first
   deployment's retrieval smoke test failed with `relation "thread_projects" does not exist`.
   Applied directly (Entra-admin AAD token as the connection password, a temporary firewall rule
   for the operator's IP, removed after). The very next request then failed with `permission
   denied for table thread_projects`: the migration created the table but never granted the
   API's own runtime role access to it. Fixed with a second migration
   (`0004_grant_thread_projects.sql`) and the same temporary-firewall-rule procedure.

2. **Demo's ADLS registry entry didn't match its real deployed corpus.** `demo_data/
   projects_registry.json`'s "demo" entry listed only `armie_demo_schedule.pdf`, while the real
   production `PDF_FILES` (D-022) carries the full 18-document SPEC-M6 corpus -- two
   independently-maintained configuration sources with nothing enforcing they match, and once
   ADLS mode resolves "demo" from the registry's `manifest.pdf_files` instead of
   `settings.pdf_files`, they silently diverged. The retrieval smoke test's Panel-E question
   returned the single-document disambiguation message instead of naming `rfi_log_047.pdf`.
   Fixed (`fix/m9-demo-corpus-registry-mismatch`, merged): `generate_demo_data.py` gained
   `DEMO_PDF_FILES` and regenerated the registry with demo's full corpus (`source_set_id` bumped
   to `demo-v2`, OD-41); `ServiceContainer._download_and_publish` fixed to create each
   `pdf_file`'s own parent directory individually (a latent bug never exercised until a
   `corpus/`-nested path was actually downloaded); the 17 remaining corpus PDFs uploaded to ADLS.

3. **ADLS-mode `document_analyzers` keyed by the nested manifest path, not the basename.**
   After fix 2 above, a *third* deployment attempt failed differently again: a blanket miss
   naming nothing, with zero `retrieval_evaluated`/`error`/`model_called` audit events in the
   live trace at all -- meaning `_retrieve_relevant_documents` ran but its own `name in
   analyzers` filter silently dropped every candidate. Root cause: `ServiceContainer.
   _load_project_sync` (this milestone's ADLS-mode `get_project` path) keyed
   `document_analyzers` by the manifest's own `corpus/rfi_log_047.pdf`-style path, while every
   other consumer of that dict -- `ServiceContainer.__init__`'s own eager, non-ADLS dict, and
   the Azure AI Search index built by `scripts/index_document_corpus.py` -- already keys by the
   bare basename. Fixed by keying `_load_project_sync`'s dict by `Path(pdf_file).name` instead,
   with a regression test that fails without the fix and passes with it.

**A fourth defect, in the smoke test itself, not the application.** With fixes 1-3 live, the
Azure AI Search retrieval smoke test finally passed -- but the multi-project ADLS smoke test
failed the first time it ever actually ran to completion: its "demo" verification question asked
about "Panel-A" expecting `44.50`, written back when demo's registry pointed at only
`armie_demo_schedule.pdf`. Fix 2 above expanded demo's registry to the full corpus, in which
"Panel-A" is SPEC-M6's own deliberate, genuine precision-failure collision across three of
demo's documents -- the deployed app correctly returned `clarification_required`; the smoke
test's own expectation was the stale part. Fixed by switching demo's check to "DB-L1-A"
(`18.50`), a board name that only ever appears in `armie_demo_schedule.pdf`; westgate's own
check keeps "Panel-A" (`51.20`), since its corpus is a single document and no collision is
possible there.

**A fifth defect, frontend-only, found by the owner's own live walkthrough after all backend
smoke tests passed.** The multi-project selector -- this milestone's whole point -- never
rendered on the real deployed app, in any session, regardless of whether ADLS mode was
genuinely on. Root cause: `App` (`apps/web/src/main.tsx`) has no gate component wrapping it --
the access-key prompt is an early `return` inside the same component -- so its `GET /api/v1/
projects` effect (empty dependency array) fired on first mount, before `sessionStorage` had a
key (a fresh tab always starts with none), always got a `401`, and latched `projects` to `[]`
via its own `.catch`; `submitApiKey` only re-invokes the metadata fetch, never that separate
effect, so the empty list was permanent for the rest of the session even after a correct key was
entered. Fixed by fetching `/api/v1/projects` from inside the metadata-load success callback
instead (the same point `ensureSessionId()` already runs from), guaranteeing it only ever fires
once the API key is already known good. No test caught this because no frontend test exercises
the fresh-session access-key-gate flow combined with the project selector -- exactly the kind of
gap this project's established live-walkthrough practice exists to catch.

**Verification.** All five defects fixed and redeployed; the full `azure-deploy.yml` run
(including the multi-project ADLS smoke test, run to completion for the first time) passed
end-to-end: `GET /api/v1/projects` lists `demo`/`westgate`; demo answers `DB-L1-A` at `18.50`;
westgate answers `Panel-A` at `51.20` (a deliberately shared board name across the two
projects' own corpora, with a different value in each, proving one project's question can never
resolve using the other's data); a demo-bound thread continued against `westgate` returns `409`.
Independently re-verified in a real browser against the redeployed app, not only via the
workflow's own smoke test: the project selector now renders both projects, switching to
`westgate` rebuilds the IFC viewer with its own geometry and resets the conversation, and a
live chat question against each project answers correctly with its own cited evidence. Full
account, including the ADLS directory-ACL three-part verification's exact commands, in
`docs/reports/2026-09-12-m9-multi-project-adls-baseline.md`.
