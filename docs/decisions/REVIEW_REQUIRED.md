# Review required

Institutional-knowledge gaps recorded here per SPEC-M1 §12. These are answered by the owner
over time; they are not inferred or resolved during implementation.

## RESOLVED by M1.5: disposition collapse / `_refuse` unreachability

Previously (through M2P1): `error`, `clarification`, and `unsupported` all surfaced as
disposition `refused` on the semantic-planning failure path, a partial violation of the
D-004 honest-failure invariant. SPEC-M1.5 (`docs/specs/SPEC-M1.5-disposition-contract-v1.md`
§4A, D-010) fixes this: `AgentService._unsupported_subresult` now maps the actual underlying
cause of a `source="unsupported"` subplan to the correct disposition (`error` /
`clarification_required` / `unsupported`) instead of hardcoding `refused`, and `_refuse`'s
own historical `refused` fallback is corrected to `unsupported` for the same reason. See
D-010 for the full mechanism and `tests/test_disposition_contract.py` for the regression
coverage.

## RESOLVED by M1.5: `heuristic_multi_plan` cross-source-join crash

`apps/api/app/agent/router.py`'s `heuristic_multi_plan` constructed
`MultiQueryPlan(subplans=[], ...)` in its own cross-source-join refusal branch, which
violated the schema's `min_length=1` and raised a `pydantic` `ValidationError` instead of
returning gracefully. Fixed in SPEC-M1.5 §4C to construct a valid single
`source="unsupported"` subplan, matching `heuristic_plan`'s own single-plan pattern. The
underlying duplication (this check, `heuristic_plan`'s copy, and
`_synthesize_multi_response`'s copy, alongside `AgentService._resolve_context`'s — the one
live, authoritative check) is mapped and documented inline at each call site rather than
removed outright, since `heuristic_plan` and `heuristic_multi_plan` are directly unit-tested
independent of the graph. `tests/test_disposition_contract.py` proves the reachability
finding directly rather than leaving it as an assertion; the former crash-pinning test is
rewritten to assert the fixed, graceful behaviour
(`test_heuristic_multi_plan_gracefully_refuses_cross_source_join`).

## Why `qwen3:30b` was selected for escalation, and whether it was ever expected to be installed

`graph.py` hardcoded `"qwen3:30b"` as the semantic-repair escalation model, absent from
`config.py`, `.env.example`, and all prior documentation (A11). SPEC-M1 makes this
configurable and opt-in via `Settings.ollama_escalation_model` (default disabled, OD-2), but
does not answer why `qwen3:30b` specifically was chosen, or whether any environment was ever
expected to hold it.

## Whether CJK handling is a committed product requirement or temporary compatibility scaffolding

`router.py`, `plan_validation.py`, and `graph.py` carry extensive hardcoded Chinese phrase
lists, grouping-keyword tables, a hardcoded clarification string, and a full parallel
Chinese answer-rendering branch (A16). SPEC-M1 M1 characterizes and pins this behaviour only
(§4.6); it does not decide whether this is a committed, maintained product surface or
scaffolding that should be redesigned, generalized, or removed.

## Why `three@0.155.0` was chosen despite the `web-ifc-three` peer range

The frontend previously pinned `three@0.155.0` while `web-ifc-three@^0.0.126` peer-requires
`three@^0.149.0`, making a clean `npm install`/`npm ci` fail (A3-A5). SPEC-M1 M1 resolves the
conflict by pinning `three@0.149.0` (D-006, OD-1), but does not establish why `0.155.0` was
chosen originally.

## M1.5 candidate: `web-ifc`, `web-ifc-three`, and the redundant asset-preparation path

Verified (A8): neither `web-ifc` nor `web-ifc-three` is imported by any application source.
Their only consumers are `scripts/copy-ifc-wasm.mjs`, which copies assets already committed
to Git; the viewer renders `THREE.BoxGeometry` from the server-computed
`/api/v1/project/viewer-elements` endpoint, not client-side IFC parsing. SPEC-M1 M1 must not
remove them (OD-7). After M1 establishes the regression net, a future milestone should
determine whether these were retained for an intended client-side IFC parsing path or are
genuinely removable dead weight.

## Why `ResponseLanguage` admits `pt-PT`, `fr`, `es` with no rendering branch

`ResponseLanguage.code` is `Literal["en", "zh-CN", "pt-PT", "fr", "es"]`, but only English and
Chinese rendering branches exist in `AgentService._natural_answer`; the other three silently
fall through to the English branch (A17). SPEC-M1 M1 characterizes this behaviour only
(§4.6/18, OD-8); a later milestone must decide explicitly whether to narrow the language
contract, implement the declared languages, or redesign language handling.

## Whether OpenAI/hybrid support is intended for active release or future experimentation

`llm_provider` accepts `ollama`, `hybrid`, and `openai`, and `providers/factory.py` selects
between `OllamaProvider` and `OpenAIProvider` accordingly, but the public release documentation
states OpenAI has not been validated end to end. SPEC-M1 M1 preserves this selection logic
unchanged and asserts its equivalence only at the factory-selection/audit-field level
(`test_provider_seam_invariance.py`); it does not decide whether OpenAI/hybrid is an active
release target.

## RESOLVED by M1.5: vision fallback did not distinguish "no matching record" from "genuinely unclear image"

Previously (M2P1): a structurally unanswerable question (no record named at all) and a
genuinely unclear image tripped the same `if result.confidence < pdf_confidence_threshold`
gate into the vision fallback, because `native_lookup`'s confidence=0.4 ("no record named")
carried no distinction from "vision might actually help here." Measured live: the
intentionally-ambiguous "total connected load" question cost 2 real model calls and ~26s
post-M2P1, versus 0 calls pre-M2P1. Fixed in SPEC-M1.5 §4B/OD-14 (D-010):
`DocumentQueryResult.miss_reason` carries this distinction forward, and `_execute_pdf`
routes a structural record-miss straight to `clarification_required` at zero model calls.
Every other below-threshold case (field absent, multiple field or record matches) is
unchanged. Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md` (the original
finding) and `tests/test_disposition_contract.py`'s Q7 regression (the fix, asserting
`fake.calls == []`).

## Pre-existing: the vision path's `ambiguity` field is surfaced to the user with no validation

`graph.py:822` and `:910` pass `location.ambiguity`/`visual_verification.rationale`
verbatim into the user-facing answer. Live-model testing observed `qwen3-vl:8b` sometimes
returning a non-informative or boolean-like value in this field (observed: `"true"`,
`"ambiguity"`) instead of a natural-language explanation. Reproduces identically on both
`main` @ `0d11ed6` and the M2P1 branch, confirming the vision prompts/logic are genuinely
unmodified by M2P1 (§4.5 item 12) -- pre-existing, not introduced or fixed by this milestone.
No fabricated value is ever presented as verified (disposition stays
`clarification_required` in every observed case), so this is a message-quality defect, not
a D-004 violation. Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md`.

## M2: `scripts/generate_demo_data.py` no longer reproduces the committed fixtures byte-for-byte

SPEC-M2's fixture work (`Tag` population on the 8 door/window instances, OD-18; a second
schedule page, §4E.2) was applied directly to the committed `demo_data/armie_demo.ifc` and
`demo_data/armie_demo_schedule.pdf`, not to `scripts/generate_demo_data.py`. This was a
deliberate scope decision, not an oversight: the script is absent from SPEC-M2 §6's affected
surfaces, no §8 acceptance criterion requires touching it, and running `make_ifc()` would
regenerate every `GlobalId` from scratch (verified: `ifcopenshell.api.run("root.create_entity",
...)` assigns a fresh random GUID per run) and silently destroy the Tag edit's fixture identity.
Re-running the script today would produce a fixture where `Tag` is unset again and the PDF has
only one page — a real, live drift between the generator and the committed fixtures. A future
milestone should either fold the `Tag` values and the second schedule page into `make_ifc()`/
`make_pdf()` directly, or explicitly document the two-step process (generate, then apply the
committed fixture edits) as the intended workflow.

## M2: reconciliation responses have no localized (non-English) answer text

`reconciliation_plan` (`router.py`) still detects `response_language="zh-CN"` from a Chinese
door/window reconciliation question (per its own detector tests), but
`_synthesize_reconciliation_response`'s answer text (`graph.py`) is a single English-only
template. Per the PR #6 review fix, the response now honestly reports
`response_language="en"` unconditionally rather than overclaiming a language the text isn't
actually written in -- but the underlying gap (no Chinese reconciliation rendering) is
unresolved, just no longer misreported. Distinct from the CJK PDF-question routing gap below
(that is source-routing detection; this is answer-text localization for one specific,
already-routed capability). A future milestone should either implement a Chinese
reconciliation template, mirroring `_natural_answer`'s existing `chinese = ...` branching
pattern, or narrow `reconciliation_plan`'s detected `response_language` to match what the
synthesis path can actually render.

## Pre-existing: PDF-question routing (`router.py:231`) is English-only

The keyword list gating the PDF-domain heuristic fast path (`"load", "circuit", "diversity",
"schedule", "breaker", "electrical"`) is English-only, so a CJK document question (e.g.
"连接负荷是多少" for "what is the connected load") never reaches it and falls to the
semantic LLM planner, which live-model testing observed misclassifying and refusing it on
both `main` @ `0d11ed6` and the M2P1 branch. Distinct from the response-language (CJK
answer-rendering) behaviour M1 already characterizes (§4.6) -- this is source-routing
detection, not answer language, and was not touched by M2P1 (§6 limits `router.py` changes
to `requested_field` only). Evidence: `docs/reports/2026-08-24-m2p1-live-model-baseline.md`.

## M2: the reconciliation detector is keyword-level, not attribute-aware

`cross_source_reconciliation_requested` matches on a door/window entity term, a comparison
verb, and a drawing/schedule reference -- it does not understand *which attribute* the
question asks about. A question like "Compare door fire ratings in the IFC and PDF schedule"
matches the detector and is routed into reconciliation, which always compares only width and
height regardless of the question's actual wording: `disposition="answered"` with correct
width/height data, no acknowledgment that fire ratings (or any other unsupported attribute)
were never checked. Verified directly: this reproduces on current `main`.

This is not a fabrication in the D-004/D-011 sense (the width/height data returned is
correct, not invented), but it is a silent attribute substitution a user could reasonably
read as "the system checked what I asked about." Candidate fix: either have the detector
require the attribute mentioned to be width/height-shaped before matching, or have
`_synthesize_reconciliation_response` name the attribute it actually compared in the answer
text so the substitution is visible rather than implicit. Not scheduled; flagging for a
future milestone's scope decision.

## RESOLVED by M4: `checkpoint_db_path` is dead configuration

Found during SPEC-M3's pre-implementation tech-debt scan (`docs/specs/SPEC-M3-azure-vertical-slice-v1.md`
§3): `apps/api/app/config.py` declares `checkpoint_db_path: Path = Path("./runtime/checkpoints.sqlite")`,
and `.env.example` documents `CHECKPOINT_DB_PATH` alongside it, but a repository-wide search of
`apps/api/app` finds no second reference to it anywhere. No LangGraph checkpointer is actually
wired to this path -- the setting exists but nothing reads it. SPEC-M3 deliberately left this
unfixed (`docs/specs/SPEC-M3-azure-vertical-slice-v1.md` §5): removing or wiring up unrelated
dead configuration was out of that milestone's scope, which was about making the existing system
deployable to Azure, not about LangGraph persistence. A future milestone should either remove
`checkpoint_db_path`/`CHECKPOINT_DB_PATH` as dead weight, or decide this is where a real
checkpointer belongs and wire one up -- both are live options; neither is decided here.

**Resolved by SPEC-M4** (`docs/decisions/README.md` D-014): removed outright, not repurposed.
SPEC-M4's `ConversationStore`/`AuditStore` persistence is a different, bespoke mechanism (not a
LangGraph checkpointer), so reusing this setting's name for it would have been more confusing than
deleting the four lines that referenced it (`config.py`, `.env.example`).

## RESOLVED by D-016/D-019: the public API has no authentication, authorization, or rate limiting

Found by an independent review of PR #9/#10 (2026-09-09, D-012), confirmed directly: any
internet caller can reach `armiem3-web`'s public FQDN and call `/api/v1/chat`, consuming Azure
OpenAI quota with no owner check, and can query or cancel *any* request by ID (`/api/v1/requests/
{id}`, `/api/v1/requests/{id}/cancel`) with no check that they originated it -- `apps/api/app/
main.py`'s route handlers take no auth dependency, and the reverse proxy (`apps/web/
default.conf.template`) adds none either. The `minReplicas: maxReplicas: 1` limit (OD-22) bounds
container count, not request volume, token spend, or per-caller abuse.

This is a real, architectural gap, not a quick patch -- SPEC-M3 never claimed to have solved it
(it was scoped as a demo vertical slice), but it should not be left running unattended or shared
publicly without at least a minimal mitigation (an access-restricted ingress rule, or Entra ID
authentication on the Container App) until real authorization is designed. A future milestone
should design who the caller model even is (anonymous demo? authenticated per-project user?)
before picking a mechanism -- this is Phase 2 scope, not a one-line fix.

**Partially resolved by SPEC-M5** (`docs/decisions/README.md` D-015, OD-28): the "caller model"
question above is answered -- shared-secret auth, not per-project user identity, matching this
project's actual demo/portfolio usage. `require_api_key` (`apps/api/app/security.py`) is required
app-wide once `api_shared_secret`/`API_SHARED_SECRET` is set; the frontend gates on it and stores
it in `sessionStorage` (switched from `localStorage` after a CodeQL finding, see SPEC-M5's §I
addendum). **Still open, explicitly, not silently dropped**: this authenticates
possession of the secret, not per-caller identity, so anyone holding it can still query or cancel
any other holder's requests on `/api/v1/requests/*` -- SPEC-M5 closes "random internet strangers
can't get in at all," not per-caller ownership. Rate limiting/abuse throttling was also not
bundled in (the owner chose shared-secret auth over the "anonymous + rate limiting" alternative
explicitly).

**Deployed and live-verified, 2026-09-09** (`docs/reports/2026-09-09-m5-azure-deployment-baseline.md`):
independently re-checked directly against the live app -- no `Authorization` header and a wrong
key both return `401`; the real key returns the same correct answer the M3/M4 baselines already
proved, unaffected by this milestone.

**Further resolved, 2026-09-10 (D-016):** the "any holder can query or cancel any other holder's
requests" gap named above is now closed. `POST /api/v1/session` issues a server-generated,
per-tab caller-correlation token; `app.state.requests` records which session created each entry;
`GET /api/v1/requests/{id}` and `POST /api/v1/requests/{id}/cancel` both 404 a session mismatch
before revealing anything. Explicit, not silently broadened: this is caller correlation, not a
new identity/authentication layer (OD-28's shared-secret choice stands unchanged), and a request
created without the session flow (a direct API script) stays unrestricted, matching pre-D-016
behaviour rather than breaking non-browser callers. Rate limiting/abuse throttling is still not
bundled in -- unchanged, still open.

**Resolved, 2026-09-11 (D-019):** rate limiting closes the last piece of this finding.
`require_rate_limit` (`apps/api/app/security.py`), applied to `/api/v1/chat` and its resume
endpoint only (the two routes that actually spend Azure OpenAI/Search quota), caps requests per
rolling minute once `rate_limit_requests_per_minute`/`RATE_LIMIT_REQUESTS_PER_MINUTE` is set,
keyed by `X-Session-Id` (falling back to the raw `Authorization` header value for callers with no
session, the same proportional compatibility choice D-016 made for the same case). In-process
only (`app/rate_limit.py`), matching OD-22's existing single-replica pin -- not a distributed
limiter, and documented as such. Unset by default, same opt-in pattern as every other optional
setting in this project. This finding (D-012 Finding 1) is now fully resolved across
authentication (D-015), per-caller ownership (D-016), and rate limiting (D-019).

## PARTIALLY RESOLVED by M4: audit/evidence is not persisted across Container App revisions

Found by the same independent review, confirmed directly: `audit_store_path` and `evidence_dir`
(`apps/api/app/config.py`) are local container filesystem paths with no volume or external store
configured in `infra/bicep/apps.bicep`. `PROJECT_STATE.md`'s existing claim that "the audit trail
persists to a local JSONL file... so audit history survives a restart" is true for a local dev
machine's persistent disk, but does not carry over to a Container App revision replacement (any
redeploy) -- the entire audit/evidence history for that container instance is lost, including any
`trace_id` a user might have saved. `AppDependencies` telemetry in Application Insights is not a
substitute: it has no domain-level audit content, only dependency call metadata.

Not a quick fix -- needs real persistence (Phase 2's already-planned PostgreSQL direction, OD-20),
not a bolted-on volume mount as a workaround. Until then, this milestone's own documentation
should not imply audit continuity survives a deployment, which it does not.

**Partially resolved by SPEC-M4** (`docs/decisions/README.md` D-014): `AuditStore`/
`ConversationStore` now have a Postgres-backed implementation (`apps/api/app/persistence/
postgres_store.py`), used whenever `DATABASE_URL` is set -- audit history and conversation
context both survive a revision replacement once the (separately provisioned, not automatic)
Postgres data tier is deployed and configured. **Still open**: `evidence_dir` (rendered PDF crops)
was explicitly excluded from SPEC-M4's scope (owner-confirmed, OD-24) and remains local-filesystem
only -- needs Azure Data Lake Storage Gen2, its own, larger milestone. Also still open: this fix
is inert until an owner deliberately deploys `infra/bicep/data.bicep` and sets `DATABASE_URL`
(SPEC-M4's cost note) -- a deployment that leaves it unset is in exactly the pre-M4 state.

**Update, 2026-09-09: deployed and live-verified.** The owner authorized the real deployment
(`docs/reports/2026-09-09-m4-azure-deployment-baseline.md`) -- `armiem3-api` now runs with
`DATABASE_URL` set against a live Azure Database for PostgreSQL Flexible Server. A real follow-up
question against the deployed app correctly used conversation context from the prior turn, and a
direct query against the live database (independent of the app) shows real `conversations`/
`audit_events` rows written by it. `evidence_dir` remains the one open item above (still
local-filesystem only, unchanged).

## M4: request-tracking/cancellation state has no cross-replica representation

Identified while scoping SPEC-M4 (`docs/decisions/README.md` D-014), not fixed: `app.state.requests`
(`apps/api/app/main.py`) stores `{"task": asyncio.current_task(), ...}` per in-flight request,
and `POST /api/v1/requests/{id}/cancel` calls `task.cancel()` directly on that in-process
coroutine object. A live `asyncio.Task` has no meaningful representation outside the event loop
that created it -- it cannot be serialized to Postgres, Redis, or anything else -- so this state
stays in-memory-only regardless of how durable conversations/audit become.

Consequence, stated plainly for the owner: OD-22's `minReplicas: maxReplicas: 1` pin
(`infra/bicep/apps.bicep`) cannot be lifted by persisting more state to a database. Doing so would
need a different cancellation *mechanism* -- e.g. a polling-based `cancel_requested` flag in a
shared store that each replica's own request-handling loop checks periodically, replacing direct
`task.cancel()` entirely -- which is real design work, not scheduled, and would also need to
reconcile with the pre-existing `asyncio.to_thread` cancellation gap (D-012/`docs/reports/
2026-09-09-m3-independent-review-and-fixes.md`: cancelling the awaited future does not stop the
worker thread already running `agent.invoke`).

## RESOLVED by owner decision: should CI run a real Postgres, and does a service container fit the "no network egress" policy?

Raised by a second independent review of SPEC-M4 (D-014 addendum): `tests/test_postgres_persistence.py`
and `tests/test_chat_persistence_failure_handling.py`'s live-database cases are skipped in CI
(gated behind `TEST_DATABASE_URL`, unset there), verified only by a developer manually running
`docker compose up postgres` locally -- which this session did, repeatedly, but CI itself never
exercises the real Postgres-backed store implementations at all.

The straightforward fix is a GitHub Actions `services: postgres:` block on the `backend` job
(runs on the runner itself, no external network reachability once started). Whether that is
consistent with `.github/workflows/ci.yml`'s own documented policy ("No Ollama, no model
downloads, no secrets, no network egress beyond package registries") is genuinely ambiguous:
pulling the `postgres:16-alpine` image is a Docker Hub fetch, which is arguably outside "package
registries" read literally, even though the running container itself then makes no further
network calls. Not decided here -- this needs an explicit owner call on how strictly that policy
clause is meant to be read, not a unilateral interpretation in either direction.

**Owner decision (2026-09-09): a `services: postgres:` block does not violate this policy.** The
owner's stated reasoning: a GitHub Actions service-container image comes from a container
registry the same way PyPI/npm packages come from a package registry, and once started the
container itself makes no further outbound network calls -- consistent with this policy's actual
intent (no live external services, no secrets, no model downloads), not a loophole in it.
Implemented in `.github/workflows/ci.yml`: the `backend` job now runs a `postgres:16-alpine`
service container, applies `apps/api/migrations/0001_conversations_and_audit_events.sql`, and
runs the full suite (including the previously `TEST_DATABASE_URL`-gated tests) against it on
every push and pull request, on all four Python versions.

## RESOLVED by M6: the multi-document/enterprise-retrieval prerequisite now has real evidence

`PROJECT_STATE.md`'s 2026-09-10 Phase 3 scoping note flagged that "enterprise retrieval" (the
originally-planned Phase 3) had no way to be evaluated: the system hardcoded exactly one PDF, so
there was no corpus to retrieve *among*, and no way to prove semantic search would beat what
already existed. SPEC-M6 (D-017) resolves this: `Settings.pdf_files`/`ServiceContainer.
document_analyzers` support a real multi-document corpus, and a new naive deterministic baseline
(`_execute_pdf_multi_document`) evidences two distinct failure modes against an 18-document
corpus -- a precision failure (the same field answerable from more than one document) and a
recall failure (a realistically-phrased question's answer exists only under different vocabulary
than any table uses). The owner explicitly rejected the first spec draft's precision-failure-only
scope as insufficient justification for Azure AI Search specifically; both failure modes are now
real, tested fixtures, not an assumption. SPEC-M7 (Azure AI Search) is gated on this evidence,
with an explicit invariant carried forward from M1/M2P1/D-009: any future retrieval step may only
perform document/page location, never answer synthesis.

## RESOLVED by M7: Azure AI Search retrieval built and evaluated against M6's evidence, on real data

Closes the gate M6 set. Azure AI Search retrieval (`docs/reports/2026-09-10-m7-azure-ai-search-
baseline.md`) is live-deployed and evaluated against M6's own precision/recall failure fixtures,
not a fresh scenario: it correctly ranks the recall-failure document (`rfi_log_047.pdf`) first for
the exact fixture question, verified end-to-end over real HTTP, and leaves the precision-failure
(collision) case unchanged, verified by call-count. Honest, checked-not-assumed nuance: hybrid
search's win on the exact fixture question is substantially attributable to lexical overlap on the
entity name, not embedding semantics alone -- a further paraphrased query with no literal overlap
confirms the genuinely semantic claim independently. `azure_search_relevance_threshold` was set
from real measured score clusters (0.025), not guessed. Still open, unaddressed by this milestone:
per `PROJECT_STATE.md`'s scoping note, ADLS Gen2 (a real document source of truth, and the
still-separate evidence-persistence gap) remains a future milestone; this one indexes directly
from `demo_data/` by design (§B, disproportionate to build a full indexer pipeline for 18
documents).
