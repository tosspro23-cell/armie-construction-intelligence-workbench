# Architecture

## Execution layers

```text
Interaction Layer
  ↓
Intent and Normalization
  ↓
Planning Layer
  ↓
Typed QueryPlan Contract
  ↓
Capability Gate
  ↓
Controlled Domain Execution
  ↓
Evidence Layer
  ↓
Verification Layer
  ↓
Audit and Observability Layer
```

The browser supplies the question plus optional selected-element and current-view context. FastAPI creates a request-scoped agent invocation. LangGraph carries short-term state through context resolution, planning, capability gating, execution, verification, and final response nodes.

## Hybrid planning

High-confidence common questions use a deterministic parser. Long-tail natural English can use the bounded Ollama semantic planner. Both routes produce the same Pydantic `QueryPlan`/`MultiQueryPlan` contract, then pass through the same validation and capability gate. Planner failure is an error; a valid but unsupported plan is explicitly unsupported.

## Deterministic BIM computation

IfcOpenShell reads entities, storey relationships, controlled quantities, and units. Counts, grouping, and bounded height extrema are computed by Python tools. The LLM never invents or calculates those facts.

## Evidence and verification

Each tool result produces evidence locators: IFC GlobalId/ExpressID or PDF page/region. Verification checks result shape, evidence presence, and independent deterministic or visual support before an answer is finalized. Without sufficient evidence the graph clarifies or refuses.

## Auditability

The UI groups existing audit events into intent, normalization, planning, execution, verification, and final response. Raw payloads remain available in a collapsed developer view. This makes provider, model, tool, parameters, evidence, verification, disposition, and latency inspectable without turning the normal answer into raw JSON.

## Multi-project workspace (SPEC-M9, D-023)

`ServiceContainer.get_project(project_id)` resolves a committed registry
(`demo_data/projects_registry.json`) to a `ProjectResources` (its own `IfcRepository` and
`DocumentAnalyzer`s), downloaded and verified once per project against Azure Data Lake Storage
Gen2 when configured (`ADLS_ACCOUNT_URL`), with a frozen `source_set_id` provenance value
recorded on every citation and audit event. `ConversationStore.bind_project` binds one thread to
exactly one project for its lifetime, enforced backend-side. This is not a hypothetical future
abstraction: four independently-isolated projects run against the real deployed slice today
(`demo`, `westgate` -- synthetic fixtures; `duplex`, `digitalhub` -- real, openly-licensed
buildings, SPEC-M13, see README.md's "Real Dataset Pack"), each with its own directory-level
POSIX ACL on the underlying storage account, verified with a minimal service principal holding
zero RBAC roles.

## Tool-calling agent and finding resolution (SPEC-M16/M17, D-061/D-063/D-064)

A second engine, selectable per conversation, replaces the fixed plan → execute → verify pipeline
above with a bounded iteration loop (`AgentService.invoke_v2`): each iteration either dispatches
every tool call the model requested this turn (`asyncio.gather`, same underlying deterministic
IFC/PDF tools as V1) or produces a final streamed answer. A narrative-consistency check
(`_narrative_consistent_with_tool_facts`) cross-references every number the final answer states
against this turn's own real tool results before the answer is shown `verified` -- a disclosed
heuristic, not a general semantic fact-checker, closed against six distinct real false-positive
shapes found by live stress-testing (D-065, D-066, D-068, D-069, D-073).

Every non-matched item a door/window reconciliation detects is promoted into a persisted
`EngineeringFinding` (`apps/api/app/finding_workflow.py`'s own enforced state machine: open →
acknowledged → action required → resolved → re-verified/closed). The V2 agent can investigate one,
making several real tool calls before submitting a typed, structured verdict via
`submit_finding_verdict` -- never a free-text guess, and the agent never writes to the real IFC
model or PDF drawing itself. A human explicitly approves or rejects the resulting proposal;
"re-verify" re-reads the real sources afresh rather than trusting either side's own say-so.

## Real Dataset Pack (SPEC-M13)

Two of the four configured projects are real, unmodified, openly-licensed buildings (Duplex
Apartment, CC BY 4.0; RWTH DigitalHub, MIT -- full attribution in each
`demo_data/projects/<name>/ATTRIBUTION.md`), used specifically because a synthetic-only fixture
cannot exercise a real building's own scale and property-richness: DigitalHub alone carries 111
real door/window openings and dozens of vendor-specific IFC properties per element, which drove
several real fixes to token-budget handling and tool-selection precision (D-067, D-070, D-072)
that a small synthetic fixture never surfaced.

Still bounded, deliberately: one project per thread, no cross-project query, and no general
`SourceRegistry`/`CapabilityRegistry` abstraction beyond what `ServiceContainer`/`ProjectResources`
already provide -- broadening that further is an explicit owner decision, not an assumed next
step.

## Azure profile (SPEC-M3/M4/M7/M8/M9, D-012/014/018/020/023)

The identical application code runs either fully local (no Azure setting configured) or as a
real cloud-native slice, opt-in per setting in `apps/api/app/config.py`:

```text
React (Azure Container App: web, public)
  -> FastAPI + LangGraph (Azure Container App: api, internal-only)
       -> Azure OpenAI (Managed Identity)                 -- interpretation/planning/vision
       -> Azure AI Search (Managed Identity)               -- retrieval-directed miss, opt-in
       -> Azure Database for PostgreSQL (Managed Identity) -- conversation + audit persistence
       -> Azure Blob Storage (Managed Identity)            -- evidence crop persistence
       -> Azure Data Lake Storage Gen2 (Managed Identity)  -- multi-project source isolation
       -> Application Insights (OpenTelemetry)             -- per-request trace, Cloud Provenance link
```

No API keys anywhere in the codebase or infrastructure -- every Azure dependency authenticates
via a Managed Identity scoped to least-privilege RBAC. Every layer above degrades cleanly to a
local, zero-cost equivalent (or is simply absent) when its own setting is unset; there is exactly
one codebase, not a fork for "the local demo" versus "the real deployment." `infra/bicep/`
provisions it; `.github/workflows/azure-deploy.yml` (manual `workflow_dispatch` only) builds,
deploys, and runs a post-deploy smoke test against the real subscription on every run, including
a guard that refuses to silently disable an already-enabled optional feature on redeploy.
