import { useState } from "react";
import { AuthedImage } from "./apiClient";
import { formatDims } from "./Findings";

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

type Citation = { evidence_id: string; source_type: string; label: string; locator: Record<string, any>; project_id?: string; source_set_id?: string; source_file?: string };
type TraceEvent = { id: string; step: string; event_type: string; summary: string; payload: Record<string, any>; actual_provider?: string; actual_model?: string; planning_mode?: string; model_call_count?: number; tool_call_count?: number; timestamp?: string };
type ReconciliationItem = { tag: string; entity_type?: string | null; storey?: string | null; ifc_width_m?: number | null; ifc_height_m?: number | null; pdf_width_m?: number | null; pdf_height_m?: number | null; status: string; detail: string };
type Response = {
  disposition: string; answer_markdown: string; citations: Citation[]; verification: { status: string; reason?: string };
  execution_metadata: Record<string, any>; reconciliation_items?: ReconciliationItem[];
};

type Stage = "Intent Understanding" | "Normalization" | "Planning" | "Execution" | "Verification" | "Final Response";

function auditStage(event: TraceEvent): Stage {
  const step = event.step.toLowerCase();
  const type = event.event_type.toLowerCase();
  if (step.includes("resolve") || type.includes("context") || type.includes("selection")) return "Intent Understanding";
  if (type.includes("coverage") || type.includes("canonical") || type.includes("normalize") || type.includes("delta") || type.includes("precedence")) return "Normalization";
  // "v2_turn_started" (SPEC-M16): V2 has no separate context-resolution/
  // normalization phase -- its first model round trip (deciding tool
  // calls) *is* "Planning" (its own Plan step subtitle already reads
  // "tool_calling planning"). Owner-reported, 2026-09-16: without this,
  // the event fell through to the unused "Final Response" bucket and V2's
  // Question/Plan steps showed no time at all.
  if (step.includes("route") || step.includes("plan") || type.includes("semantic") || type.includes("repair") || step.startsWith("v2_turn_started")) return "Planning";
  if (step.includes("execute") || type.includes("tool") || type.includes("subplan")) return "Execution";
  // type.includes("consistency") (owner-reported, 2026-09-16): a plain
  // "execution_consistency" event_type/step (emitted by the same shared
  // _execute_ifc consistency check both V1 and V2 use) doesn't contain
  // "verif", so it fell through to "Final Response" -- an unused bucket --
  // instead of Verification, alongside its sibling result_shape_verification
  // event which already matched. Affects both engines equally; not V2-only.
  if (step.includes("verif") || type.includes("verif") || type.includes("evidence") || type.includes("consistency")) return "Verification";
  // "v2_turn_continued" deliberately falls through to here, not Planning
  // (owner-reported, 2026-09-16, second pass): stageTimingsMs below keeps
  // only the *first* timestamp seen per stage, so tagging this the same
  // as "v2_turn_started" would not add a new boundary -- Planning's own
  // firstSeen would still be the earlier v2_turn_started event, and this
  // whole (often multi-second, LLM-generated-answer) call would keep
  // bleeding into Verification's window exactly as before. Landing it in
  // "Final Response" instead -- a stage with no earlier event of its own
  // this turn -- gives it a real boundary, so Verification's number
  // finally reflects only its own near-instant, no-model-call checks.
  return "Final Response";
}

// Owner-requested, 2026-09-14: the Result step showed one total latency
// with no way to tell which step actually spent the time. There is no
// per-event duration recorded anywhere (AuditEvent.duration_ms exists but
// is never populated) -- this is a client-side approximation from each
// event's own `timestamp`, already present on every event, not a new
// backend measurement. Each stage's span runs from its own first event
// until the next stage-with-events' first event (or the trace's last
// event, for whichever stage ran last) -- this charges the "thinking
// time" between a stage's last audit call and the next stage's first one
// to the stage that was actually running during that gap, which a naive
// max-minus-min *within* one stage's own events would miss entirely for
// any stage that only ever logs a single event.
function stageTimingsMs(trace: TraceEvent[]): Partial<Record<Stage, number>> {
  const firstSeen = new Map<Stage, number>();
  let lastSeen = -Infinity;
  for (const event of trace) {
    if (!event.timestamp) continue;
    const t = new Date(event.timestamp).getTime();
    if (Number.isNaN(t)) continue;
    const stage = auditStage(event);
    if (!firstSeen.has(stage) || t < firstSeen.get(stage)!) firstSeen.set(stage, t);
    if (t > lastSeen) lastSeen = t;
  }
  const ordered = [...firstSeen.entries()].sort((a, b) => a[1] - b[1]);
  const durations: Partial<Record<Stage, number>> = {};
  ordered.forEach(([stage, start], index) => {
    const end = index + 1 < ordered.length ? ordered[index + 1][1] : lastSeen;
    durations[stage] = Math.max(0, end - start);
  });
  return durations;
}

function formatMs(ms: number | undefined): string | null {
  if (ms === undefined) return null;
  return ms < 1000 ? `~${Math.round(ms)} ms` : `~${(ms / 1000).toFixed(1)} s`;
}

// D-047: owner-reported, 2026-09-15 -- every step's latency was formatted
// identically ("~120 ms"), including the Result step's, which is not a
// per-step approximation like the other five but the real, backend-
// measured, whole-request latency -- indistinguishable from the others by
// look alone, so it read as if it might be one more per-step number
// rather than the total they add up to. `stepMs` labels the five
// per-stage approximations explicitly; `totalMs` labels Result's number
// explicitly as the total, never sharing formatMs's bare output with them.
function stepMs(ms: number | undefined): string | null {
  const formatted = formatMs(ms);
  return formatted ? `${formatted} this step` : null;
}

function totalMs(ms: number | undefined): string | null {
  const formatted = formatMs(ms);
  return formatted ? `Total ${formatted} (all steps)` : null;
}

function citationFacts(citation: Citation): Array<[string, string]> {
  return Object.entries(citation.locator || {})
    .filter(([key, value]) => key !== "evidence_crop" && key !== "localized" && value !== null && value !== undefined && value !== "")
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

// Found live, 2026-09-14: the owner's own Azure Portal opened the Cloud
// Provenance link's Logs blade into its "Queries hub" picker with no data
// shown -- a per-account Portal default this URL cannot control. This
// button offers the same KQL as plain, copyable text so pasting it
// directly into that picker's own search box still reaches the data.
function CopyQueryButton({ query }: { query: string }) {
  const [copied, setCopied] = useState(false);
  return <button
    type="button"
    className="cloud-provenance-copy"
    title="Copy this trace's Application Insights query -- paste it into the 'Queries hub' search box if the link above doesn't run it automatically."
    onClick={async () => {
      try {
        await navigator.clipboard.writeText(query);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } catch {
        window.prompt("Copy this query manually:", query);
      }
    }}
  >{copied ? "Copied ✓" : "Copy query"}</button>;
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
  const reconciliationItems = latest.reconciliation_items || [];
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
  const stageTimings = stageTimingsMs(trace);
  // Plan's own StepTrace below already combines Planning + Normalization
  // events into one list -- their latencies are combined here too, so the
  // subtitle's number matches what that step's trace actually covers.
  const planLatencyMs = stageTimings["Planning"] === undefined && stageTimings["Normalization"] === undefined
    ? undefined
    : (stageTimings["Planning"] ?? 0) + (stageTimings["Normalization"] ?? 0);

  return <section className="decision-story">
    <h2>Decision Trace</h2>

    {/* D-028, owner-requested: one click from here into this specific
        request's own Application Insights telemetry in the Azure Portal --
        `meta.cloud_trace_url` is only present when the deployment has
        azure_tenant_id/app_insights_resource_id configured (opt-in, same
        pattern as every other optional Azure setting); otherwise this
        stays plain text exactly as before this fix, never a broken link. */}
    {(meta.project_id || projectId) && <div className="cloud-provenance">
      <span className="cloud-provenance-icon" aria-hidden="true">☁️</span>
      <span>Cloud provenance — Project <strong>{meta.project_id || projectId}</strong> · Frozen source version <strong>{meta.source_set_id || "—"}</strong></span>
      <span className="cloud-provenance-actions">
        {meta.cloud_trace_url && <a href={meta.cloud_trace_url} target="_blank" rel="noreferrer" title="Opens Application Insights Logs, pre-filtered to this request's trace ID. If a 'Queries hub' dialog opens first, use 'Copy query' instead and paste it into that dialog's own search box.">View in Application Insights →</a>}
        {meta.cloud_trace_query && <CopyQueryButton query={meta.cloud_trace_query} />}
      </span>
    </div>}

    <ol className="story-steps">
      <li className="story-step">
        <StepHeader number={1} icon="❓" title="Question" subtitle={[meta.normalized_request ? null : "as asked", stepMs(stageTimings["Intent Understanding"])].filter(Boolean).join(" · ") || undefined} />
        <p className="story-step-body">{meta.normalized_request || "—"}</p>
        <StepTrace events={byStage("Intent Understanding")} />
      </li>

      <li className="story-step">
        {/* model_call_count is a whole-request total (it also counts calls
            made during Execution, e.g. AI Search retrieval) -- showing it
            here implied those calls happened during planning even when
            planning_mode was
            "heuristic" (zero model calls). Found live, 2026-09-13: a user
            went looking for "the model call the Plan step claims" inside
            this step's own trace and, correctly, couldn't find one. The
            total now lives on the Result step below, which is the one
            step that actually summarizes the whole request. */}
        <StepHeader number={2} icon="🧭" title="Plan" subtitle={[`${meta.planning_mode || "—"} planning`, stepMs(planLatencyMs)].filter(Boolean).join(" · ")} />
        {subplans.length === 0 ? <p className="story-step-body empty">No plan recorded.</p> : <ul className="plan-list">
          {subplans.map((plan, index) => <li key={plan.subtask_id || index}>
            <span className="plan-op">{plan.operation || plan.intent || "—"}{plan.entity_type ? ` · ${plan.entity_type}` : ""}</span>
            {plan.rationale && <span className="plan-rationale">“{plan.rationale}”</span>}
          </li>)}
        </ul>}
        <StepTrace events={[...byStage("Planning"), ...byStage("Normalization")]} />
      </li>

      <li className="story-step">
        <StepHeader number={3} icon="⚙️" title="Execution" subtitle={[`source: ${meta.source || "—"}`, `${meta.tool_call_count || 0} tool call(s)`, stepMs(stageTimings["Execution"])].filter(Boolean).join(" · ")} />
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
        {/* SPEC-M11 SS D: AgentResponse.reconciliation_items has been
            computed and returned by every reconciliation answer since
            SPEC-M2, but nothing rendered it -- a stranger reading a
            reconciliation answer's prose summary had no way to see the
            actual per-tag comparison behind it. This is that table; the
            persisted, human-reviewable version of each non-matched row
            lives in the Findings tab (main.tsx). */}
        {reconciliationItems.length > 0 && <div className="reconciliation-table">
          <table><thead><tr><th>Tag</th><th>Status</th><th>IFC</th><th>PDF</th></tr></thead><tbody>
            {reconciliationItems.map((item) => <tr key={item.tag} className={item.status !== "matched" ? "mismatch" : undefined}>
              <td>{item.tag}</td>
              <td>{item.status.replace(/_/g, " ")}</td>
              <td>{formatDims(item.ifc_width_m, item.ifc_height_m)}</td>
              <td>{formatDims(item.pdf_width_m, item.pdf_height_m)}</td>
            </tr>)}
          </tbody></table>
        </div>}
        {/* Collapsed by default, one click to open -- matches the same
            collapsed-summary-then-expandable-detail pattern already used
            for each step's own raw trace and the AI Search relevance
            table. Previously every citation's full card (including its
            evidence-crop image) rendered open at once, which was the
            first thing a stranger saw regardless of how many citations an
            answer had. Owner-requested, 2026-09-14. */}
        {citations.length === 0 ? <p className="story-step-body empty">No citations for this answer.</p> : <div className="evidence-cards">
          {citations.map((citation) => <details key={stableCitationKey(citation)} className="evidence-card">
            <summary><span className={`evidence-badge ${citation.source_type}`}>{citation.source_type}</span><span className="evidence-label">{citation.label}</span></summary>
            {citation.locator?.evidence_crop && <AuthedImage className={`evidence-crop ${citation.locator.localized === false ? "unlocalized" : ""}`} src={`/api/v1/evidence/${citation.locator.evidence_crop}`} alt="Cited PDF evidence crop" />}
            {citation.locator?.localized === false && <p className="evidence-unlocalized-note">Exact location on the page could not be confidently determined — showing the full page for manual review.</p>}
            <dl className="citation-facts">{citationFacts(citation).slice(0, 4).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
            <button type="button" className="evidence-jump" onClick={() => onOpenCitation(citation)}>Jump to this evidence →</button>
          </details>)}
        </div>}
      </li>

      <li className="story-step">
        <StepHeader number={5} icon="✅" title="Verification" subtitle={[latest.verification.status, stepMs(stageTimings["Verification"])].filter(Boolean).join(" · ")} />
        <p className={`story-step-body verification-reason ${latest.verification.status}`}>{latest.verification.reason || (latest.verification.status === "passed" ? "Every value was independently checked against its own source before being presented." : "—")}</p>
        <StepTrace events={byStage("Verification")} />
      </li>

      <li className="story-step">
        <StepHeader number={6} icon="🏁" title="Result" subtitle={[meta.latency_ms ? totalMs(meta.latency_ms) : null, `${meta.model_call_count || 0} model call(s) total`].filter(Boolean).join(" · ")} />
        <p className="story-step-body">Disposition: <strong>{latest.disposition.replace(/_/g, " ")}</strong></p>
        {/* D-047: owner-requested breakdown showing the per-step numbers
            actually add up to the total above -- the five per-step
            timings are a client-side approximation (see stageTimingsMs's
            own comment), the total is the real, backend-measured request
            latency; both are labeled as such here rather than left to
            look like the same kind of number. */}
        {meta.latency_ms && <p className="story-step-body latency-breakdown">
          Breakdown (approximate): {[
            ["Question", stageTimings["Intent Understanding"]],
            ["Plan", planLatencyMs],
            ["Execution", stageTimings["Execution"]],
            ["Verification", stageTimings["Verification"]],
          ].map(([label, ms]) => [label, formatMs(ms as number | undefined)] as const)
            .filter(([, formatted]) => formatted)
            .map(([label, formatted]) => `${label} ${formatted}`)
            .join(" + ")} = <strong>Total {formatMs(meta.latency_ms)}</strong> (backend-measured)
        </p>}
        <StepTrace events={byStage("Final Response")} />
      </li>
    </ol>

    <details className="raw-trace">
      <summary>Raw event list ({trace.length})</summary>
      <ol>{trace.map((event) => <li key={event.id}><strong>{event.step} · {event.event_type}</strong><span>{event.summary}</span><details><summary>Payload</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></li>)}</ol>
    </details>
  </section>;
}
