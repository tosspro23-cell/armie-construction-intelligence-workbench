import { AuthedImage } from "./apiClient";

// Interview/demo-facing redesign (2026-09-13, user request): the previous
// "Evidence Inspector" + "Audit Trail" were two disconnected sections --
// evidence buried three <details> levels deep, six audit stages collapsed
// by default, and no visible link between a citation and the cloud
// resources actually backing it (project_id/source_set_id, added to
// execution_metadata/citations in D-023 SS F, was computed but never
// surfaced anywhere in the UI at all). A stranger walking up to this app
// had no fast, obvious path from "here's an answer" to "here's the proof,
// and here's where it actually lives." This renders one linear top-to-
// bottom trace instead: Question -> Plan -> Execution -> Evidence ->
// Verification -> Result -- each step showing its own headline result
// inline, with that step's own raw trace events/JSON one click away
// right underneath it, not lumped into one undifferentiated pile at the
// bottom.

type Citation = { evidence_id: string; source_type: string; label: string; locator: Record<string, any>; project_id?: string; source_set_id?: string };
type TraceEvent = { id: string; step: string; event_type: string; summary: string; payload: Record<string, any>; actual_provider?: string; actual_model?: string; planning_mode?: string; model_call_count?: number; tool_call_count?: number };
type Response = {
  disposition: string; answer_markdown: string; citations: Citation[]; verification: { status: string; reason?: string };
  execution_metadata: Record<string, any>;
};

type Stage = "Intent Understanding" | "Normalization" | "Planning" | "Execution" | "Verification" | "Final Response";

function auditStage(event: TraceEvent): Stage {
  const step = event.step.toLowerCase();
  const type = event.event_type.toLowerCase();
  if (step.includes("resolve") || type.includes("context") || type.includes("selection")) return "Intent Understanding";
  if (type.includes("coverage") || type.includes("canonical") || type.includes("normalize") || type.includes("delta") || type.includes("precedence")) return "Normalization";
  if (step.includes("route") || step.includes("plan") || type.includes("semantic") || type.includes("repair")) return "Planning";
  if (step.includes("execute") || type.includes("tool") || type.includes("subplan")) return "Execution";
  if (step.includes("verif") || type.includes("verif") || type.includes("evidence")) return "Verification";
  return "Final Response";
}

function citationFacts(citation: Citation): Array<[string, string]> {
  return Object.entries(citation.locator || {})
    .filter(([key, value]) => key !== "evidence_crop" && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [key.replace(/_/g, " "), Array.isArray(value) ? value.join(", ") : String(value)]);
}

function stableCitationKey(citation: Citation) {
  const locator = citation.locator || {};
  return citation.source_type === "ifc"
    ? `${citation.source_type}:${locator.global_id}:${locator.query_operation}`
    : citation.source_type === "pdf"
      ? `${citation.source_type}:${locator.page}:${JSON.stringify(locator.bbox)}:${locator.field}`
      : `${citation.source_type}:${locator.snapshot_id}`;
}

function StepHeader({ number, icon, title, subtitle }: { number: number; icon: string; title: string; subtitle?: string }) {
  return <div className="story-step-header">
    <span className="story-step-number">{number}</span>
    <span className="story-step-icon" aria-hidden="true">{icon}</span>
    <div><strong>{title}</strong>{subtitle && <span className="story-step-subtitle">{subtitle}</span>}</div>
  </div>;
}

// Per-step "how do I know that" toggle: this step's own trace events,
// collapsed by default -- the same JSON already available in the app's
// audit store, just reachable one click under the step it actually
// belongs to instead of a single undifferentiated pile at the bottom.
function StepTrace({ events }: { events: TraceEvent[] }) {
  if (events.length === 0) return null;
  return <details className="step-trace">
    <summary>{events.length} trace event(s) for this step</summary>
    <ol>{events.map((event) => <li key={event.id}>
      <strong>{event.summary}</strong>
      {event.actual_model && <small>Model: {event.actual_provider}/{event.actual_model}</small>}
      <details><summary>Raw JSON</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
    </li>)}</ol>
  </details>;
}

export function DecisionStory({ latest, trace, projectId, onOpenCitation }: {
  latest: Response | undefined; trace: TraceEvent[]; projectId: string | undefined;
  onOpenCitation: (citation: Citation) => void;
}) {
  if (!latest) {
    return <section className="decision-story empty-story">
      <h2>Decision Trace</h2>
      <p className="empty">Ask a question to see its plan, evidence, and verification laid out step by step here.</p>
    </section>;
  }

  const meta = latest.execution_metadata || {};
  const citations = Array.from(new Map<string, Citation>((latest.citations || []).map((item) => [stableCitationKey(item), item])).values());
  const subplans: any[] = meta.subplans || [];
  const retrievalEvent = trace.find((event) => event.event_type === "retrieval_evaluated");
  // Independent-review-style finding, self-caught while demo-testing
  // (2026-09-13): a question that hits the *precision*-collision path
  // (the same field genuinely answerable from more than one document,
  // e.g. "Panel-B") also produces a clarification naming documents --
  // easy to mistake for the *recall*-failure/retrieval-directed miss
  // (e.g. "Panel-E") the Azure AI Search card exists for, since neither
  // one's message alone makes which mechanism fired obvious, and only
  // the second ever populates retrievalEvent. Both code paths already
  // emit a "clarification_requested" audit event with a distinct,
  // accurate summary (graph.py's own two literal strings) -- surfaced
  // here whenever there's no retrieval card, so the absence of Azure AI
  // Search is always explained by the backend's own words, never just
  // silently empty.
  const clarificationEvent = trace.find((event) => event.event_type === "clarification_requested");
  const byStage = (stage: Stage) => trace.filter((event) => auditStage(event) === stage);

  return <section className="decision-story">
    <h2>Decision Trace</h2>

    {(meta.project_id || projectId) && <div className="cloud-provenance">
      <span className="cloud-provenance-icon" aria-hidden="true">☁️</span>
      <span>Cloud provenance — Project <strong>{meta.project_id || projectId}</strong> · Frozen source version <strong>{meta.source_set_id || "—"}</strong></span>
    </div>}

    <ol className="story-steps">
      <li className="story-step">
        <StepHeader number={1} icon="❓" title="Question" subtitle={meta.normalized_request ? undefined : "as asked"} />
        <p className="story-step-body">{meta.normalized_request || "—"}</p>
        <StepTrace events={byStage("Intent Understanding")} />
      </li>

      <li className="story-step">
        <StepHeader number={2} icon="🧭" title="Plan" subtitle={`${meta.planning_mode || "—"} planning · ${meta.model_call_count || 0} model call(s)`} />
        {subplans.length === 0 ? <p className="story-step-body empty">No plan recorded.</p> : <ul className="plan-list">
          {subplans.map((plan, index) => <li key={plan.subtask_id || index}>
            <span className="plan-op">{plan.operation || plan.intent || "—"}{plan.entity_type ? ` · ${plan.entity_type}` : ""}</span>
            {plan.rationale && <span className="plan-rationale">“{plan.rationale}”</span>}
          </li>)}
        </ul>}
        <StepTrace events={[...byStage("Planning"), ...byStage("Normalization")]} />
      </li>

      <li className="story-step">
        <StepHeader number={3} icon="⚙️" title="Execution" subtitle={`source: ${meta.source || "—"} · ${meta.tool_call_count || 0} tool call(s)`} />
        {retrievalEvent ? <div className="retrieval-evidence">
          <p className="retrieval-summary">
            <strong>Azure AI Search retrieval considered</strong> — {retrievalEvent.payload.documents_evaluated?.length || 0} document(s) scored,
            highest {Math.max(...(retrievalEvent.payload.documents_evaluated || [{ score: 0 }]).map((d: { score: number }) => d.score)).toFixed(4)}
            {" "}(threshold {retrievalEvent.payload.relevance_threshold})
          </p>
          <details className="retrieval-detail">
            <summary>Show relevance table</summary>
            <span className="empty">A document is only named in the answer if its score clears the threshold.</span>
            <table><thead><tr><th>Document</th><th>Relevance score</th><th>Named in answer?</th></tr></thead><tbody>
              {(retrievalEvent.payload.documents_evaluated || []).map((doc: { filename: string; score: number }) => {
                const directed: string[] = retrievalEvent.payload.directed_candidates || [];
                return <tr key={doc.filename} className={directed.includes(doc.filename) ? "above-threshold" : undefined}>
                  <td>{doc.filename}</td><td>{doc.score.toFixed(4)}</td><td>{directed.includes(doc.filename) ? "Yes" : "No"}</td>
                </tr>;
              })}
            </tbody></table>
          </details>
        </div> : clarificationEvent && <p className="retrieval-absent">No Azure AI Search evidence for this step — {clarificationEvent.summary}</p>}
        <StepTrace events={byStage("Execution")} />
      </li>

      <li className="story-step story-step-evidence">
        <StepHeader number={4} icon="📄" title="Evidence" subtitle={`${citations.length} citation(s)`} />
        {citations.length === 0 ? <p className="story-step-body empty">No citations for this answer.</p> : <div className="evidence-cards">
          {citations.map((citation) => <article key={stableCitationKey(citation)} className="evidence-card" onClick={() => onOpenCitation(citation)}>
            <header><span className={`evidence-badge ${citation.source_type}`}>{citation.source_type}</span><span className="evidence-label">{citation.label}</span></header>
            {citation.locator?.evidence_crop && <AuthedImage className="evidence-crop" src={`/api/v1/evidence/${citation.locator.evidence_crop}`} alt="Cited PDF evidence crop" />}
            <dl className="citation-facts">{citationFacts(citation).slice(0, 4).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
            <span className="evidence-jump">Jump to this evidence →</span>
          </article>)}
        </div>}
      </li>

      <li className="story-step">
        <StepHeader number={5} icon="✅" title="Verification" subtitle={latest.verification.status} />
        <p className={`story-step-body verification-reason ${latest.verification.status}`}>{latest.verification.reason || (latest.verification.status === "passed" ? "Every value was independently checked against its own source before being presented." : "—")}</p>
        <StepTrace events={byStage("Verification")} />
      </li>

      <li className="story-step">
        <StepHeader number={6} icon="🏁" title="Result" subtitle={meta.latency_ms ? `${meta.latency_ms} ms` : undefined} />
        <p className="story-step-body">Disposition: <strong>{latest.disposition.replace(/_/g, " ")}</strong></p>
        <StepTrace events={byStage("Final Response")} />
      </li>
    </ol>

    <details className="raw-trace">
      <summary>Raw event list ({trace.length})</summary>
      <ol>{trace.map((event) => <li key={event.id}><strong>{event.step} · {event.event_type}</strong><span>{event.summary}</span><details><summary>Payload</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></li>)}</ol>
    </details>
  </section>;
}
