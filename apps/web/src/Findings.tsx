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
type AgentProposal = {
  proposal_id: string; proposed_width_m: number | null; proposed_height_m: number | null;
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
  ifc_width_m: number | null; ifc_height_m: number | null; pdf_width_m: number | null; pdf_height_m: number | null;
  evidence_refs: string[]; pending_proposal: AgentProposal | null;
  created_at: string; updated_at: string; last_actor_session_id: string | null;
  history: FindingHistoryEntry[];
};

// Client-side mirror of app/finding_workflow.py's `_TRANSITIONS` table, for
// which buttons to show only -- a stale copy here just risks an extra 409
// round-trip, never an actually-illegal transition, since the server
// re-validates every action independently.
const LEGAL_ACTIONS: Partial<Record<FindingStatus, Array<{ action: string; label: string }>>> = {
  open: [{ action: "acknowledge", label: "Acknowledge" }],
  acknowledged: [
    { action: "start_action", label: "Start action" },
    { action: "waive", label: "Waive" },
    { action: "mark_false_positive", label: "Mark false positive" },
  ],
  action_required: [{ action: "resolve", label: "Mark resolved" }],
};

export function formatDims(width: number | null | undefined, height: number | null | undefined): string {
  if (width == null && height == null) return "—";
  return `${width != null ? width.toFixed(2) : "?"} × ${height != null ? height.toFixed(2) : "?"} m`;
}

function errorMessage(err: unknown): string {
  const raw = (err as Error).message || "Request failed.";
  try { return JSON.parse(raw).detail || raw; } catch { return raw; }
}

export function Findings({ projectId }: { projectId: string | undefined }) {
  const [findings, setFindings] = useState<EngineeringFinding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    api<EngineeringFinding[]>(`/api/v1/findings${query}`).then((value) => { setFindings(value); setError(null); }).catch((err) => setError(errorMessage(err)));
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  async function act(findingId: string, action: string) {
    setBusyId(findingId); setError(null);
    try {
      const updated = await api<EngineeringFinding>(`/api/v1/findings/${findingId}/transition`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action }),
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

  // SPEC-M17 §4D: this call can take a real V2 model round trip (a genuine
  // tool-calling investigation, not a cached lookup) -- busyId already
  // disables the triggering button for exactly this reason, same as every
  // other action here.
  async function proposeResolution(findingId: string) {
    setBusyId(findingId); setError(null);
    try {
      const updated = await api<EngineeringFinding>(`/api/v1/findings/${findingId}/propose-resolution`, { method: "POST" });
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
            {(LEGAL_ACTIONS[finding.status] || []).map(({ action, label }) => (
              <button key={action} type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, action)}>{label}</button>
            ))}
            {finding.status === "resolved" && <button type="button" disabled={busyId === finding.finding_id} onClick={() => reverify(finding.finding_id)} title="Re-runs the actual IFC/PDF comparison for this tag -- a human's 'resolved' click is never trusted on its own.">Re-check now</button>}
            {/* SPEC-M17 §4D: additive, not a replacement for "Mark resolved"
                above -- a human can still resolve manually with no agent
                involvement at all, exactly as SPEC-M11 shipped it. */}
            {finding.status === "action_required" && finding.finding_type === "dimension_mismatch" && !finding.pending_proposal &&
              <button type="button" disabled={busyId === finding.finding_id} onClick={() => proposeResolution(finding.finding_id)} title="Asks V2's tool-calling agent to investigate this mismatch and propose which value is correct -- you still decide whether to approve it.">Ask agent to investigate</button>}
          </div>
          {finding.pending_proposal && <div className="finding-proposal">
            <div className="finding-proposal-header">
              <strong>Agent-proposed resolution</strong>
              <span className={finding.pending_proposal.verification.status}>{finding.pending_proposal.verification.status.replace(/_/g, " ")}</span>
            </div>
            <dl className="citation-facts">
              <div><dt>Proposed dimensions</dt><dd>{formatDims(finding.pending_proposal.proposed_width_m, finding.pending_proposal.proposed_height_m)}</dd></div>
            </dl>
            <p className="story-step-body">{finding.pending_proposal.rationale}</p>
            {finding.pending_proposal.verification.status === "unverified" &&
              <p className="unverified-caveat">⚠ This proposal's own numbers could not be fully confirmed against the agent's own tool results — please double-check before approving it.</p>}
            {finding.pending_proposal.citations.length > 0 &&
              <p className="story-step-body">{finding.pending_proposal.citations.length} citation(s) from the investigating turn.</p>}
            <div className="finding-actions">
              <button type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, "approve_proposal")}>Approve</button>
              <button type="button" disabled={busyId === finding.finding_id} onClick={() => act(finding.finding_id, "reject_proposal")}>Reject</button>
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
