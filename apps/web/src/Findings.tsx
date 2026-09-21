import { useCallback, useEffect, useState } from "react";
import { api } from "./apiClient";

// SPEC-M11: surfaces the persisted EngineeringFinding lifecycle -- a
// human-reviewable promotion of reconciliation's own non-matched items
// (see app/finding_workflow.py for the state machine this mirrors for UI
// gating only; the server is the actual enforcer of every transition).

type FindingStatus = "open" | "acknowledged" | "action_required" | "resolved" | "verified_closed" | "waived" | "false_positive";
// SPEC-M17 §4A: mirrors AgentProposal/Citation/VerificationStatus's own
// shape server-side -- kept local and minimal (not imported from main.tsx)
// the same way this whole file already keeps its own EngineeringFinding
// mirror self-contained.
type FindingCitation = { evidence_id: string; source_type: string; label: string; locator: Record<string, any> };
// Independent-review finding, 2026-09-19 (D-064 item 2): `verdict` is the
// model's own structured conclusion (submitted via submit_finding_verdict,
// not inferred from free text) -- the only field that can express what a
// missing_in_pdf/missing_in_ifc investigation actually concludes, since
// "is this a real omission" isn't a width or a height. `null` only for a
// turn that never reached the tool at all.
type AgentProposal = {
  proposal_id: string; proposed_width_m: number | null; proposed_height_m: number | null;
  verdict: "dimension_confirmed" | "genuine_omission" | "found_under_different_reference" | "inconclusive" | null;
  verdict_basis: string | null;
  rationale: string; citations: FindingCitation[];
  verification: { status: "verified" | "unverified" | "failed" | "not_applicable"; reason?: string | null };
  trace_id: string; generated_at: string;
};
type FindingHistoryEntry = {
  id: string; from_status: FindingStatus | null; to_status: FindingStatus; actor_session_id: string | null;
  note: string | null; proposal_snapshot: AgentProposal | null; at: string;
};
type EngineeringFinding = {
  finding_id: string; project_id: string; source_set_id: string; trace_id: string; tag: string;
  finding_type: "dimension_mismatch" | "missing_in_pdf" | "missing_in_ifc";
  severity: "low" | "medium" | "high"; status: FindingStatus; detail: string;
  // D-070: the real IFC entity type (IfcDoor/IfcWindow) this tag was
  // matched to by reconciliation -- null for missing_in_ifc (there is no
  // IFC element to have read a type from) or for a finding persisted
  // before this field existed.
  entity_type: string | null;
  ifc_width_m: number | null; ifc_height_m: number | null; pdf_width_m: number | null; pdf_height_m: number | null;
  evidence_refs: string[]; pending_proposal: AgentProposal | null;
  created_at: string; updated_at: string; last_actor_session_id: string | null;
  history: FindingHistoryEntry[];
};

// Client-side mirror of app/finding_workflow.py's `_TRANSITIONS` table, for
// which buttons to show only -- a stale copy here just risks an extra 409
// round-trip, never an actually-illegal transition, since the server
// re-validates every action independently.
const LEGAL_ACTIONS: Partial<Record<FindingStatus, Array<{ action: string; label: string; title?: string }>>> = {
  open: [{ action: "acknowledge", label: "Acknowledge" }],
  acknowledged: [
    { action: "start_action", label: "Start action" },
    { action: "waive", label: "Waive" },
    { action: "mark_false_positive", label: "Mark false positive" },
  ],
  // Independent-review finding, 2026-09-19 (D-064 item 3): distinguishes
  // "a human accepted this determination" from "the actual IFC/PDF source
  // was corrected" -- resolve/approve never touch the real files (SPEC-M17
  // §6's own invariant), and this button is the one place that could
  // otherwise be misread as "the system fixed it." "Re-check now" below
  // is the actual closed-loop proof this milestone's own re-verify
  // guarantee relies on.
  action_required: [{
    action: "resolve", label: "Mark resolved",
    title: "Records that you've addressed this manually -- it does not modify the IFC model or PDF drawing itself. Use \"Re-check now\" afterward to confirm the real sources actually agree.",
  }],
};

export function formatDims(width: number | null | undefined, height: number | null | undefined): string {
  if (width == null && height == null) return "—";
  return `${width != null ? width.toFixed(2) : "?"} × ${height != null ? height.toFixed(2) : "?"} m`;
}

// Independent-review finding, 2026-09-19 (D-064 item 2): human-readable
// labels for AgentProposal.verdict -- kept as an explicit switch (not a
// generic "replace underscores with spaces") so a future verdict value
// added server-side without a matching case here shows as itself
// (readable, if unstyled) rather than silently rendering nothing.
function formatVerdict(verdict: AgentProposal["verdict"]): string {
  switch (verdict) {
    case "dimension_confirmed": return "Dimension confirmed";
    case "genuine_omission": return "Genuine omission";
    case "found_under_different_reference": return "Found under a different reference";
    case "inconclusive": return "Inconclusive";
    default: return verdict ?? "—";
  }
}

function errorMessage(err: unknown): string {
  const raw = (err as Error).message || "Request failed.";
  try { return JSON.parse(raw).detail || raw; } catch { return raw; }
}

// SPEC-M17, amended 2026-09-18 (owner decision: one agent, one place it
// visibly works). `onInvestigate` hands off to main.tsx's own V2
// conversation flow instead of this component calling a dedicated
// endpoint and rendering its own copy of the agent's reasoning -- this
// component goes back to being a pure workflow-status list: it can
// trigger an investigation and show its *result* (a compact proposed
// value + verification badge to approve/reject), but the actual
// multi-step reasoning only ever streams in the Conversation panel.
// `refreshToken` is bumped by main.tsx once that turn's result has
// landed on the backend finding, since this component's own `findings`
// state has no other way to learn about it. `investigating` (main.tsx's
// own `busy`) disables triggering a second investigation while one V2
// turn is already in flight -- the Conversation panel is a single shared
// stream, not one per finding.
export function Findings({ projectId, onInvestigate, refreshToken, investigating }: {
  projectId: string | undefined;
  onInvestigate: (finding: { finding_id: string; tag: string }) => void;
  refreshToken: number;
  investigating: boolean;
}) {
  const [findings, setFindings] = useState<EngineeringFinding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    api<EngineeringFinding[]>(`/api/v1/findings${query}`).then((value) => { setFindings(value); setError(null); }).catch((err) => setError(errorMessage(err)));
  }, [projectId]);

  useEffect(() => { load(); }, [load, refreshToken]);

  // Independent-review finding, 2026-09-19: `proposalId` is required for
  // approve_proposal/reject_proposal (server-enforced) -- it identifies
  // exactly which AgentProposal this click reviewed, so a proposal
  // replaced by a concurrent investigation between page-load and this
  // click is caught as a 409 instead of silently approving/rejecting
  // whatever happens to be live by the time the request lands.
  async function act(findingId: string, action: string, proposalId?: string) {
    setBusyId(findingId); setError(null);
    try {
      const updated = await api<EngineeringFinding>(`/api/v1/findings/${findingId}/transition`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ action, ...(proposalId ? { proposal_id: proposalId } : {}) }),
      });
      setFindings((current) => (current || []).map((f) => (f.finding_id === findingId ? updated : f)));
    } catch (err) { setError(errorMessage(err)); } finally { setBusyId(null); }
  }

  async function reverify(findingId: string) {
    setBusyId(findingId); setError(null);
    try {
      const updated = await api<EngineeringFinding>(`/api/v1/findings/${findingId}/reverify`, { method: "POST" });
      setFindings((current) => (current || []).map((f) => (f.finding_id === findingId ? updated : f)));
    } catch (err) { setError(errorMessage(err)); } finally { setBusyId(null); }
  }

  if (findings === null) return <section className="findings"><h2>Findings</h2><p className="empty">Loading findings…</p></section>;

  return <section className="findings">
    <div className="findings-header"><h2>Findings</h2><button type="button" onClick={load}>Refresh</button></div>
    {error && <p className="runtime-error" role="alert">{error}</p>}
    {findings.length === 0
      ? <p className="empty">No findings for this project — reconciliation hasn't detected a door/window mismatch yet, or every one has already been waived, marked a false positive, or verified closed.</p>
      : <div className="finding-cards">
        {findings.map((finding) => <details key={finding.finding_id} className="finding-card">
          <summary>
            <span className={`finding-badge status-${finding.status}`}>{finding.status.replace(/_/g, " ")}</span>
            <span className={`finding-badge severity-${finding.severity}`}>{finding.severity}</span>
            <span className="finding-tag">{finding.tag}</span>
            {finding.entity_type && <span className="finding-entity-type">{finding.entity_type}</span>}
            <span className="finding-type">{finding.finding_type.replace(/_/g, " ")}</span>
          </summary>
          <p className="story-step-body">{finding.detail}</p>
          <dl className="citation-facts">
            <div><dt>IFC dimensions</dt><dd>{formatDims(finding.ifc_width_m, finding.ifc_height_m)}</dd></div>
            <div><dt>PDF dimensions</dt><dd>{formatDims(finding.pdf_width_m, finding.pdf_height_m)}</dd></div>
            <div><dt>Evidence</dt><dd>{finding.evidence_refs.length} citation(s) from the detecting response</dd></div>
            <div><dt>Last updated</dt><dd>{new Date(finding.updated_at).toLocaleString()}</dd></div>
          </dl>
          <div className="finding-actions">
            {(LEGAL_ACTIONS[finding.status] || []).map(({ action, label, title }) => (
              <button key={action} type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, action)} title={title}>{label}</button>
            ))}
            {finding.status === "resolved" && <button type="button" disabled={busyId === finding.finding_id} onClick={() => reverify(finding.finding_id)} title="Re-runs the actual IFC/PDF comparison for this tag -- a human's 'resolved' click is never trusted on its own.">Re-check now</button>}
            {/* SPEC-M17 §4D: additive, not a replacement for "Mark resolved"
                above -- a human can still resolve manually with no agent
                involvement at all, exactly as SPEC-M11 shipped it.
                `investigating` (main.tsx's shared `busy`) disables this
                while a V2 turn is already streaming -- one Conversation
                panel, one investigation at a time. Widened 2026-09-19
                (owner decision, live-testing session) from
                dimension_mismatch only to all three SPEC-M11 finding
                types -- mirrors the server's own INVESTIGABLE_FINDING_TYPES
                (apps/api/app/finding_workflow.py); kept as an explicit
                literal set here rather than "!== undefined" so a future
                fourth finding type doesn't silently start showing this
                button before the server-side question-builder/guard is
                updated for it too. */}
            {finding.status === "action_required" && (["dimension_mismatch", "missing_in_pdf", "missing_in_ifc"] as const).includes(finding.finding_type) && !finding.pending_proposal &&
              <button type="button" disabled={busyId === finding.finding_id || investigating} onClick={() => onInvestigate(finding)} title="Asks V2's tool-calling agent to investigate this mismatch in the Conversation panel and propose which value is correct -- you still decide whether to approve it.">Ask agent to investigate</button>}
          </div>
          {/* SPEC-M17, amended 2026-09-18, then 2026-09-19 (independent-
              review finding): the agent's reasoning streams live in the
              Conversation panel when a proposal is generated, but a
              reviewer coming back later -- after a refresh, the next day,
              or a different person entirely -- has no way to replay that.
              The compact summary below stays the primary view; the
              <details> beneath it makes the proposal genuinely self-
              contained (rationale + evidence, from the finding's own
              persisted AgentProposal) so approval never has to depend on
              "you happened to still have the chat open." */}
          {finding.pending_proposal && <div className="finding-proposal">
            <div className="finding-proposal-header">
              <strong>Agent-proposed resolution</strong>
              <span className={finding.pending_proposal.verification.status}>{finding.pending_proposal.verification.status.replace(/_/g, " ")}</span>
            </div>
            <dl className="citation-facts">
              {/* Independent-review finding, 2026-09-19 (D-064 item 2):
                  a dimension_mismatch verdict has an actual number to show;
                  missing_in_pdf/missing_in_ifc conclude something
                  categorical instead ("genuine omission" is not a width),
                  so it gets its own row rather than a permanently-empty
                  "Proposed dimensions: —". */}
              {finding.pending_proposal.verdict && <div><dt>Verdict</dt><dd>{formatVerdict(finding.pending_proposal.verdict)}</dd></div>}
              {(finding.pending_proposal.proposed_width_m != null || finding.pending_proposal.proposed_height_m != null) &&
                <div><dt>Proposed dimensions</dt><dd>{formatDims(finding.pending_proposal.proposed_width_m, finding.pending_proposal.proposed_height_m)}</dd></div>}
              <div><dt>Evidence</dt><dd>{finding.pending_proposal.citations.length} citation(s) from the investigation</dd></div>
            </dl>
            {finding.pending_proposal.verification.status === "unverified" &&
              <p className="unverified-caveat">⚠ This proposal's own numbers could not be fully confirmed against the agent's own tool results — please double-check before approving it.</p>}
            <details className="finding-proposal-detail">
              <summary>Show full reasoning and evidence ({finding.pending_proposal.citations.length} citation(s))</summary>
              {finding.pending_proposal.verdict_basis && <p className="finding-proposal-verdict-basis"><strong>Basis:</strong> {finding.pending_proposal.verdict_basis}</p>}
              <p className="finding-proposal-rationale">{finding.pending_proposal.rationale}</p>
              {finding.pending_proposal.citations.length > 0 && <ul className="finding-proposal-citations">
                {finding.pending_proposal.citations.map((citation) => <li key={citation.evidence_id}>
                  <span className={`evidence-badge ${citation.source_type}`}>{citation.source_type}</span> {citation.label}
                </li>)}
              </ul>}
              <p className="finding-proposal-meta">Generated {new Date(finding.pending_proposal.generated_at).toLocaleString()} · also streamed live in the Conversation panel when this was created</p>
            </details>
            <div className="finding-actions">
              <button type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, "approve_proposal", finding.pending_proposal!.proposal_id)} title="Records that you accept this conclusion -- it does not modify the IFC model or PDF drawing itself. Use &quot;Re-check now&quot; afterward to confirm the real sources actually agree once someone has made the correction.">Approve</button>
              <button type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, "reject_proposal", finding.pending_proposal!.proposal_id)} title="Discards this specific proposal (kept in history for the record) -- the finding stays action_required so it can be investigated again or resolved manually.">Reject</button>
            </div>
          </div>}
          <details className="step-trace"><summary>{finding.history.length} history entrie(s)</summary>
            <ol>{finding.history.map((entry) => <li key={entry.id}>
              <strong>{entry.from_status ? `${entry.from_status.replace(/_/g, " ")} → ` : ""}{entry.to_status.replace(/_/g, " ")}</strong>
              <small>{new Date(entry.at).toLocaleString()}{entry.actor_session_id ? ` · by session ${entry.actor_session_id.slice(0, 8)}` : " · system"}{entry.note ? ` · ${entry.note}` : ""}</small>
            </li>)}</ol>
          </details>
        </details>)}
      </div>}
  </section>;
}
