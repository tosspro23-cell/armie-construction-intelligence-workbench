# SPEC-M16 — V2: a full, independently-selectable tool-calling agent, benchmarked against V1

## Objective

Build a complete, standalone conversational engine ("V2": a tool-calling agent with real
multi-turn memory) as a **fully independent alternative** to the existing system ("V1": today's
heuristic-fast-path + single-shot semantic planner, entirely untouched) -- selectable per question
via a visible workbench toggle, so the two can be directly compared on answer quality *and*
latency, side by side, in the same conversation. V2 must understand a question, decide which
deterministic tools answer it (calling several in parallel when independent), maintain genuine
recent-turn conversational memory, and feel responsive in the way a consumer conversational AI
product does -- while remaining exactly as fact-strict as V1: every number V2 states must come
from an actual tool call this turn, never a model-generated guess. Nothing about D-001 ("Models
interpret a request and explain verified results; they do not calculate or invent BIM facts")
changes for either engine.

## Rationale

Owner-initiated, 2026-09-16, continuing a design discussion begun after a session's worth of
Chinese-language keyword-gap fixes (SPEC-M14/M15, D-059, D-060). Two decisions refine this spec's
first draft:

**V2 must be a full, standalone engine, not a fallback that only runs when V1 gives up.** The
owner's actual goal is a real, apples-to-apples benchmark -- the same question answered by both
engines, compared on quality and speed. A design where V2 only ever sees the questions V1 already
couldn't answer would never produce that comparison (V2 would never be measured against the
common-case questions V1 already answers well, which is exactly where a fair speed comparison
matters most). V2 therefore attempts every question independently, from a clean start, when
explicitly selected -- it does not consult V1's heuristics or semantic planner first.

**V2's responsiveness is an explicit, first-class requirement, not a nice-to-have.** The owner's
stated bar is a multi-turn experience "close to" ChatGPT/Claude. This is a real engineering
requirement with a real, honest tension to name up front: a tool-calling turn is fundamentally one
or more full network round trips to a model, which cannot be as fast as V1's already-in-process,
zero-network-call heuristic path for a simple question -- no design choice erases that physical
gap. What *does* meaningfully close the *perceived* gap, the way real chat products do it, is
token streaming (the answer appears progressively instead of all at once after a long silent
wait) plus visible tool-use progress (so a multi-step turn reads as "the assistant is working," not
"the assistant is stuck") -- neither exists anywhere in this stack today (Verified current-state
assumptions). This is genuinely new infrastructure, not a configuration change, and is treated as
such in this spec's scope and effort, not tacked on as an afterthought.

**The rest of this spec's original reasoning is unchanged and still the foundation:** three
independent, already-tracked findings in `docs/decisions/REVIEW_REQUIRED.md` (CJK handling's
scaffolding-vs-requirement question; English-only PDF routing; the reconciliation detector's
keyword-level, not attribute-aware, matching) all name the same underlying problem -- a fixed
keyword/pattern table deciding what a question means cannot keep up with real phrasing variety no
matter how many terms are added to it, as SPEC-M14/M15 themselves just demonstrated twice. And the
alternative explicitly considered and declined earlier in this same conversation -- having the
model compute facts itself from retrieved text, safety-netted by a second model's review -- was
rejected because it trades away the one guarantee that differentiates this system from a
fluent-but-occasionally-wrong chatbot: exact, reproducible, Python/IfcOpenShell-computed facts, in
a domain where a wrong count or dimension has real downstream cost. Tool-calling keeps that
guarantee fully intact in V2: every tool is a thin wrapper over the *same* deterministic execution
V1 already uses: the model's only job is deciding which tool(s) answer the question and handing
back already-verified facts -- a more flexible, iterative version of the same kind of decision
`structured()` already makes today, not a new kind of trust placed in the model.

## Verified current-state assumptions

- `ModelProvider` (`apps/api/app/providers/base.py`) exposes only `structured()` (single-shot
  structured output) and `vision_structured()` -- **no tool-calling method, and no streaming
  method, exist today.** Both are genuinely new provider-layer surface.
- **No streaming/incremental-response infrastructure exists anywhere in this stack** -- confirmed
  by inspection: `apps/api/app/main.py`'s `/api/v1/chat` returns a single, complete
  `AgentResponse`; `apps/web/src/main.tsx`'s request helper is a single `fetch`-await-render
  cycle with no incremental-rendering mechanism to extend. A "feels like Claude/ChatGPT"
  experience is new backend (SSE or chunked streaming) and new frontend (progressive rendering)
  work, not a flag.
- `AzureOpenAIProvider` already uses the standard Chat Completions API (not the newer Responses
  API, switched away from during SPEC-M3 after a live 404), which natively supports both
  `tools=[...]` and `stream=True` -- both are new call shapes against infrastructure already in
  place, not a new API surface to integrate. Streaming a tool-calling turn has a real limit worth
  naming now: a tool-call request itself must be fully received before its arguments can be
  parsed and executed (a partial function-call JSON fragment cannot be acted on) -- streaming
  buys real, visible responsiveness on the *final* natural-language answer and on surfacing
  intermediate tool-use progress as discrete events, not on shortening the number of model round
  trips a multi-tool turn genuinely needs.
- `ConversationStore` (`apps/api/app/persistence/conversation_store.py`, SPEC-M4 §A) persists only
  a small, structured `context` dict per thread (`active_entity_type`, `active_storey`,
  `previous_subplans`, etc.) -- there is no raw turn-by-turn message history anywhere in this
  system today. "Real conversational memory" is new storage, not a config toggle on existing
  storage.
- `IfcRepository.execute`/`DocumentAnalyzer`'s operations (`count`, `group_by`, `get_properties`,
  `aggregate_quantity`, `space_distance`, `extract_field`, `inspect_view`, reconciliation) are
  already boundary-clean deterministic functions -- these become V2's tool definitions directly;
  no new computation logic is needed, only a new calling convention into the same code.
- `QueryPlan`/`MultiQueryPlan` (`apps/api/app/schemas/models.py`) remain the execution-layer
  contract V2's tools resolve into internally -- evidence, verification, and disposition logic
  (all of which operate on `QueryPlan`/`IfcQueryResult`, not on how the plan was produced) need no
  change to keep working for V2-produced plans.
- Ollama's function-calling support is model-dependent and not verified for any model this project
  currently uses -- V2 does not assume it works there (see Explicitly excluded scope).

## Allowed scope

### A. Tool schema definitions (own commit)

A new module (e.g. `apps/api/app/agent/tools.py`) defining one tool per existing deterministic
operation this system already supports (`count_elements`, `group_elements_by_storey`,
`get_element_properties`, `aggregate_quantity`, `space_distance`, `extract_pdf_field`,
`inspect_current_view`, `reconcile_doors_windows`), each with a JSON-schema parameter definition
mirroring the equivalent `QueryPlan` fields exactly -- a tool call's parameters construct the
*same* `QueryPlan` shape this system already validates and executes, so `capability_gate`,
`verify_execution_consistency`, and evidence construction are reused unchanged.

### B. `ModelProvider.tool_call` (with streaming) and the Azure implementation (own commit)

A new Protocol method returning an async sequence of typed events (a tool-call request with
parsed arguments; a streamed answer-token chunk; a final-answer-complete marker), so both the
tool-selection loop and progressive answer rendering share one interface.
`AzureOpenAIProvider` implements it against the same `AsyncAzureOpenAI` client `structured()`
already uses, passing `tools=` and `stream=True` for the final answer-generation call only (tool-
selection calls are not streamed -- see Invariants for why). `OllamaProvider`/`OpenAIProvider`
raise a clear `NotImplementedError` naming this phase's scope. `FakeModelProvider` gets a
scriptable streaming-capable implementation, matching this project's existing convention for
`structured()`.

### C. V2 as a full, independent engine, selected per question (own commit)

A new `engine: Literal["v1", "v2"] = "v1"` field on `ChatRequest`. `engine="v1"` (the default,
and every existing caller's behavior when the field is omitted) is **completely unchanged** --
today's heuristic-fast-path-first, semantic-planner-second flow, byte-for-byte. `engine="v2"`
routes to a new, separate execution path that runs the tool-calling loop from the very start of
the turn -- it does **not** first attempt V1's heuristic patterns or semantic planner (a clean,
comparable, standalone engine; not a hybrid that would confound the benchmark this exists to
enable). A hard cap (`Settings.tool_calling_max_iterations: int = 6`, matching the existing
bounded-retry precedent of semantic repair/escalation) prevents an unbounded call loop; the model
is instructed and the tool schema designed so the common case resolves in one round trip (a
single tool call, or several called in parallel -- see §F). Every individual tool call and its
result is logged through the existing `_audit` mechanism, exactly like every V1 planning/execution
step, so V2 turns appear in the Decision Trace panel with the same rigor.

### D. Recent-turn conversational memory, for V2 (own commit)

`ConversationStore`'s schema gains a bounded turn history (`Settings.conversation_memory_turns:
int = 6`, raw `question`/`answer_markdown` pairs) -- both `InMemoryConversationStore` and
`PostgresConversationStore` (a new column/table, additive migration) implement it, oldest turns
evicted once the cap is reached. This history feeds V2's own prompt as real conversation context,
letting it resolve a follow-up like "and the windows?" from actual prior dialogue. V1's existing
narrow structured-context mechanism (`active_entity_type` etc.) is untouched and remains V1-only.

### E. Latency and stability engineering (own commit)

- **Parallel tool execution**: when the model requests multiple independent tool calls in one
  round (e.g., "how many doors and windows"), execute them concurrently (`asyncio.gather`), not
  sequentially.
- **Token streaming for the final answer, plus visible tool-use progress**: once every tool call
  for a turn has resolved and been verified, the final natural-language synthesis streams to the
  client token-by-token; each tool call in progress emits a lightweight status event ("Counting
  IfcDoor elements…") the frontend renders as a transient line, mirroring how Claude/ChatGPT show
  tool-use steps before the final streamed answer.
- **Bounded failure handling**: a malformed tool-call request or a transient model-call error gets
  one bounded retry, then an honest `disposition="error"` -- never a silent hang, never a
  fabricated recovery, matching this project's existing disposition-honesty invariant.
- **A live-measured V1-vs-V2 latency report** (not an assumed number): p50/p95 total turn time and
  (once streaming lands) time-to-first-token, both engines, same representative question set --
  reported honestly, matching this project's own established practice (D-044, D-047, D-056) of
  measuring before claiming. No specific target latency is committed to in this spec ahead of that
  real data; a draft goal to validate against (not a promise): time-to-first-token under ~2s for a
  single-tool-call turn.

### F. Frontend: engine toggle, streaming render, per-turn labeling (own commit)

- A visible toggle in the workbench (e.g. next to the existing "Source: Auto" control): "Engine:
  V1 / V2 (agent)", defaulting to V1, sent as `engine` on the chat request -- switchable per
  question, so the same conversation thread can carry both engines' answers for direct comparison.
- Each assistant turn in the Conversation panel is labeled with which engine answered it (and,
  once §E's benchmark exists, its measured latency), so a side-by-side comparison is legible
  without cross-referencing the audit trail.
- V2 responses render progressively (SSE or chunked `fetch`/`ReadableStream` consumption): tool-
  use status lines appear and clear as calls resolve, then the final answer's tokens append as
  they stream in -- verified live in the real browser (per this session's own established
  verification standard), not just unit-tested.

### G. Tests + a representative eval set (own commit)

- Provider-level: `FakeModelProvider.tool_call` scripted to request a sequence of tool calls
  (including a parallel-call case and a runaway-loop case proving the iteration cap actually
  stops execution) and to stream a scripted final answer in chunks.
- The same ~15-20-question representative set (English and Chinese; count/group/aggregate/
  distance/property-lookup/reconciliation) run through **both** engines: V1's real current
  disposition for each (unchanged, some legitimately `unsupported`) and V2's answer for each
  (correct, zero fabricated facts -- every number traceable to a real tool-call result, asserted
  the same way `test_failure_path_evals.py` already asserts absence of fabrication).
- At least one genuine multi-turn scenario resolved correctly via V2's recent-turn memory that
  V1's structured-context mechanism alone could not resolve.
- A streaming-specific test proving tokens/events arrive incrementally, not all at once.

## Explicitly excluded scope

- **No automatic engine selection or routing.** The engine is the user's explicit, visible choice
  via the toggle (§F) -- V2 is never silently substituted for V1 based on some internal heuristic
  (e.g., "try V1, fall back to V2 on unsupported") in this phase. That kind of hybrid routing is a
  later, separate decision, deliberately not built here because it would confound the clean A/B
  benchmark this phase exists to enable.
- **No claim that V2 matches V1's raw speed on simple/common questions in this phase.** V1's
  heuristic path is an in-process function call with no network round trip; V2 is fundamentally a
  network+model turn. The target is a *conversational-AI-grade* experience (streaming-driven
  perceived responsiveness, per §E), not literal parity with a zero-network-call shortcut -- this
  expectation is stated now, before the benchmark runs, not adjusted after seeing the numbers.
- **No MCP or external-system integration.** Owner-decided in this conversation: the internal
  tool-calling architecture first; connecting to an external system is a separate, later decision.
- **No unbounded conversation history.** A fixed-turn cap (`conversation_memory_turns`, default 6)
  only.
- **No removal or deprecation of V1.** It remains completely unchanged, and is the only engine
  used by any caller that omits the new `engine` field -- exactly today's behavior, exactly
  today's tests, exactly today's deployed behavior.
- **No autonomous action-taking** (writing back to the IFC model, external systems, or anything
  beyond read-only query tools) -- every V2 tool in scope here is read-only, matching every
  capability this system has today.
- **Ollama tool-calling is not a supported claim of this phase** -- `NotImplementedError` is the
  honest behavior, not a gap to silently work around.
- **No "faithfulness" verification step for V2's final natural-language summary** (checking it
  doesn't overstate what tool results support) is designed in this phase -- flagged for a
  follow-up phase rather than bolted on without its own consideration.

## Affected surfaces

- `apps/api/app/providers/base.py` -- new `ModelProvider.tool_call` Protocol method (streaming).
- `apps/api/app/providers/azure_openai_provider.py` -- implementation.
- `apps/api/app/providers/ollama_provider.py`, `openai_provider.py` -- explicit
  `NotImplementedError`.
- `apps/api/app/agent/tools.py` -- new; tool schema definitions.
- `apps/api/app/agent/graph.py` -- new, separate V2 execution path; V1's existing `_route` and
  every node it reaches are untouched.
- `apps/api/app/schemas/models.py` -- `ChatRequest.engine` field.
- `apps/api/app/main.py` -- an SSE/chunked-streaming-capable path for `engine="v2"` requests
  (`engine="v1"` keeps today's plain-JSON response, unchanged).
- `apps/api/app/config.py` -- `tool_calling_max_iterations`, `conversation_memory_turns`.
- `apps/api/app/persistence/conversation_store.py`, `postgres_store.py` -- turn-history storage;
  new migration file.
- `apps/web/src/main.tsx` (or a new component) -- engine toggle, SSE/stream consumption,
  progressive rendering, per-turn engine/latency labeling.
- `tests/fakes/fake_provider.py` -- `FakeModelProvider.tool_call`.
- New test file(s) for the V2 path, the representative eval set, and streaming.
- `docs/decisions/README.md` D-061; `PROJECT_STATE.md` M16 entry.

No change to `IfcRepository`, `DocumentAnalyzer`, `plan_validation.py`'s validators, evidence
construction, verification logic, or any part of V1's existing execution path.

## Invariants

- Every fact V2 states is the return value of an actual tool call this turn -- never a number the
  model states without a corresponding call, checked the same way `test_failure_path_evals.py`
  already checks fabrication-absence for V1.
- Tool execution is 100% the existing deterministic Python/IfcOpenShell code, unchanged (D-001
  fully intact for both engines).
- **Streaming only ever carries already-verified content.** Tool calls, their execution, and
  result verification all happen *before* any token is streamed to the client -- what streams is
  only the final natural-language synthesis of already-checked facts, never the model's
  intermediate "thinking" or an unverified draft. This is a deliberate design choice, not an
  implementation detail: it is what lets V2 be fast-*feeling* (progressive rendering) without
  weakening the honesty guarantee that no unverified claim ever reaches the user, in any form.
- A hard, enforced cap on tool-call iterations per turn -- never an unbounded loop.
- `engine="v1"` (the default, and every existing caller's unmodified behavior) leaves every
  existing code path, test, and deployed behavior byte-for-byte unchanged.
- Every tool call and its result appears in the audit trail, exactly like every V1
  planning/execution step -- no new "invisible" reasoning for V2 either.
- Both engines produce the same `AgentResponse` shape (citations, verification, disposition) --
  the audit trail needs no changes to render either one; only the Conversation panel gains the
  engine label and streaming render (§F).
- The engine used for a turn is always explicit and visibly labeled -- never silently switched or
  ambiguous to the person reading the conversation back.

## Acceptance criteria

- The ~15-20-question representative eval set (§G) is answered correctly through V2, zero
  fabricated facts, in a local/test environment.
- The same eval set through V1 (`engine="v1"` or omitted) reproduces today's exact dispositions
  unchanged -- proving V2 is additive, not a behavior change to V1.
- The iteration cap is proven to actually stop a runaway sequence (a test, not an assumption).
- A genuine multi-turn follow-up is resolved correctly via V2's recent-turn memory that V1's
  structured-context mechanism alone could not resolve.
- A real, measured (not assumed) V1-vs-V2 latency comparison report exists, covering the same
  question set, honestly stating what was found relative to the draft target in §E.
- Streaming is verified live in the real browser: tokens/status events render incrementally, the
  engine toggle correctly switches behavior, and each turn is correctly labeled with its engine.
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests
  scripts`, and `(cd apps/web && npm run build)` all clean after each lettered subsection.

## Documentation requirements

- `docs/decisions/README.md` D-061, referencing this spec and OD-50's resolution.
- `PROJECT_STATE.md` M16 entry.
- The live V1-vs-V2 latency/quality benchmark report (`docs/reports/`), matching this project's
  established reporting format and rigor (e.g. `docs/reports/2026-09-10-m7-azure-ai-search-
  baseline.md`).
- `docs/decisions/REVIEW_REQUIRED.md`: mark the three findings named in Rationale (CJK scaffolding-
  vs-requirement, English-only PDF routing, keyword-level reconciliation) as "addressed by
  SPEC-M16's V2 engine, pending real rollout/benchmark data," with a forward reference.

## Git / stop conditions

- Branch `feat/m16-v2-tool-calling-agent`; one commit per lettered subsection (A-G).
- Stop and report rather than proceeding on own judgment if: Azure OpenAI's real tool-calling or
  streaming behavior (tested live, not just read from documentation) has a material limitation not
  anticipated here (e.g., streaming interacting badly with tool-calling in a way that forces
  buffering the whole response anyway, defeating the responsiveness goal); if the representative
  eval set's real V2 pass rate is low enough to suggest the model needs prompt-engineering work
  beyond this phase's scope; if the live latency benchmark shows V2 meaningfully missing the draft
  target with no clear further optimization available within this phase's scope (report the real
  numbers and ask, rather than silently lowering the bar); or if extending `ConversationStore`'s
  schema surfaces a real migration/compatibility issue with `PostgresConversationStore`'s existing
  production data.

## Owner decisions

- **OD-50** (decided across this conversation, recorded here per this repo's convention): V2 is
  built as a full, independent, standalone engine attempting every question from a clean start
  when explicitly selected -- not a fallback that only runs on V1's leftovers -- specifically so a
  fair, direct V1-vs-V2 benchmark (quality and latency, same questions) is possible. Selection is
  an explicit, visible per-question UI toggle in this phase, never automatic/silent routing. V1 is
  not modified, degraded, or deprecated by this work in any way. Token streaming and visible
  tool-use progress are in scope as first-class requirements (not deferred polish), because a
  credible "V2 could become the primary path" story requires V2 to feel responsive, not only to
  answer correctly. Bounded recent-turn conversational memory (default 6 turns) is in scope; MCP/
  external-system integration is explicitly deferred to a later, separate decision.
