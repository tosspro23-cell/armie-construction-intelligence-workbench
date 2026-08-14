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

## Extensibility boundary

The current release intentionally exposes one active project workspace. Future `ProjectWorkspace`, `SourceDescriptor`, `SourceRegistry`, and `CapabilityRegistry` abstractions can support multiple sources without weakening the typed planning and capability-gate boundary.
