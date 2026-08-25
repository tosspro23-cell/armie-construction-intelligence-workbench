import React, { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { IfcViewer, ViewerStatus } from "./IfcViewer";
import "./styles.css";

type SourcePreference = "auto" | "ifc" | "pdf" | "viewer_snapshot";
type WorkspaceTab = "bim" | "drawing" | "snapshot";
type Citation = { evidence_id: string; source_type: string; label: string; locator: Record<string, any> };
type TraceEvent = { id: string; step: string; event_type: string; summary: string; payload: Record<string, any>; actual_provider?: string; actual_model?: string; planning_mode?: string; model_call_count?: number; tool_call_count?: number };
type Response = {
  thread_id: string; trace_id: string; disposition: "answered" | "partially_answered" | "clarification_required" | "refused" | "error" | "timeout" | "cancelled";
  answer_markdown: string; citations: Citation[]; verification: { status: string; reason?: string };
  execution_metadata: Record<string, any>;
};
type Selected = { globalId?: string; expressId?: number; type?: string; name?: string };
type ConversationTurn = { id: string; user: string; assistant: Response; timestamp: string; trace: TraceEvent[] };

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
};

function stableCitationKey(citation: Citation) {
  const locator = citation.locator || {};
  return citation.source_type === "ifc"
    ? `${citation.source_type}:${locator.global_id}:${locator.query_operation}`
    : citation.source_type === "pdf"
      ? `${citation.source_type}:${locator.page}:${JSON.stringify(locator.bbox)}:${locator.field}`
      : `${citation.source_type}:${locator.snapshot_id}`;
}

function citationFacts(citation: Citation): Array<[string, string]> {
  return Object.entries(citation.locator || {})
    .filter(([key, value]) => key !== "evidence_crop" && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [key.replace(/_/g, " "), Array.isArray(value) ? value.join(", ") : String(value)]);
}

function stageLabel(event: TraceEvent): string {
  const step = event.step.toLowerCase();
  if (step.includes("resolve") || event.event_type === "context_resolved") return "Understand";
  if (step.includes("route") || step.includes("plan") || event.event_type.includes("coverage")) return "Plan";
  if (step.includes("capability") || event.event_type.includes("capability")) return "Capability Check";
  if (step.includes("execute") || event.event_type.includes("tool")) return "Execute";
  if (step.includes("verif") || event.event_type.includes("verification")) return "Verify";
  return "Respond";
}

const AUDIT_STAGES = ["Intent Understanding", "Normalization", "Planning", "Execution", "Verification", "Final Response"] as const;
type AuditStage = typeof AUDIT_STAGES[number];

function auditStage(event: TraceEvent): AuditStage {
  const step = event.step.toLowerCase();
  const type = event.event_type.toLowerCase();
  if (step.includes("resolve") || type.includes("context") || type.includes("selection")) return "Intent Understanding";
  if (type.includes("coverage") || type.includes("canonical") || type.includes("normalize") || type.includes("delta") || type.includes("precedence")) return "Normalization";
  if (step.includes("route") || step.includes("plan") || type.includes("semantic") || type.includes("repair")) return "Planning";
  if (step.includes("execute") || type.includes("tool") || type.includes("subplan")) return "Execution";
  if (step.includes("verif") || type.includes("verif") || type.includes("evidence")) return "Verification";
  return "Final Response";
}

function App() {
  const [metadata, setMetadata] = useState<Record<string, any> | null>(null);
  const [threadId, setThreadId] = useState<string | undefined>();
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [selectionCleared, setSelectionCleared] = useState(false);
  const [snapshot, setSnapshot] = useState<string | null>(null);
  const [snapshotCleared, setSnapshotCleared] = useState(false);
  const [sourcePreference, setSourcePreference] = useState<SourcePreference>("auto");
  const [tab, setTab] = useState<WorkspaceTab>("bim");
  const [drawingZoom, setDrawingZoom] = useState(1);
  const [drawingEvidence, setDrawingEvidence] = useState<{ bbox?: number[]; board?: string; field?: string; page?: number } | null>(null);
  const drawingEvidenceRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [apiState, setApiState] = useState<"loading" | "ready" | "unavailable">("loading");
  const [apiError, setApiError] = useState("");
  const [viewerStatus, setViewerStatus] = useState<ViewerStatus>({ phase: "initializing", message: "Loading application…" });
  const [requestStage, setRequestStage] = useState("idle");
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const cancelledRef = useRef(false);
  const timelineRef = useRef<HTMLDivElement>(null);

  const handleSelection = useCallback((element: Selected | null) => { setSelected(element); setSelectionCleared(false); }, []);
  useEffect(() => {
    api<Record<string, any>>("/api/v1/project/metadata").then((value) => { setMetadata(value); setApiState("ready"); })
      .catch((error: Error) => { console.error(error); setApiState("unavailable"); setApiError("The local API is unavailable. Start FastAPI on port 8000 and reload."); });
  }, []);

  const status = useMemo(() => {
    if (apiState === "loading") return "Loading application…";
    if (apiState === "unavailable") return "API unavailable";
    if (!metadata?.ifc_available) return "IFC source unavailable";
    return viewerStatus.phase === "ready" ? "Viewer ready" : viewerStatus.message;
  }, [apiState, metadata, viewerStatus]);
  const latest = turns.length ? turns[turns.length - 1].assistant : undefined;
  const citations = useMemo<Citation[]>(() => Array.from(new Map<string, Citation>((latest?.citations || []).map((item: Citation) => [stableCitationKey(item), item])).values()), [latest]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || busy) return;
    const requestId = crypto.randomUUID();
    const controller = new AbortController();
    controllerRef.current = controller;
    cancelledRef.current = false;
    setActiveRequestId(requestId); setRequestStage("queued"); setBusy(true);
    const progressTimer = window.setInterval(() => {
      api<{ stage?: string }>(`/api/v1/requests/${requestId}`).then((state) => setRequestStage(state.stage || "running")).catch(() => undefined);
    }, 600);
    try {
      const response = await api<Response>("/api/v1/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        request_id: requestId, thread_id: threadId, question, source_preference: sourcePreference,
        viewer_context: {
          selected_global_ids: selected?.globalId ? [selected.globalId] : [],
          selected_express_ids: selected?.expressId === undefined ? [] : [selected.expressId],
          selected_entity_type: selected?.type || null, selected_display_name: selected?.name || null,
          camera_pose: {}, snapshot_id: snapshot ? `snapshot-${Date.now()}` : null,
          screenshot_base64: snapshot ? snapshot.split(",")[1] : null,
          selection_cleared: selectionCleared,
          snapshot_cleared: snapshotCleared,
          target_global_id: selected?.globalId || null,
          target_entity_type: selected?.type || null,
          target_visible: selected?.type === "IfcStair" || selected?.type === "IfcStairFlight",
        },
      }), signal: controller.signal });
      const responseTrace = await api<TraceEvent[]>(`/api/v1/traces/${response.trace_id}`);
      if (cancelledRef.current) return;
      setThreadId(response.thread_id); setTurns((current) => [...current, { id: response.trace_id, user: question.trim(), assistant: response, timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), trace: responseTrace }]); setQuestion("");
      setTrace(responseTrace);
    } catch (error) {
      if ((error as Error).name !== "AbortError") { console.error(error); setApiState("unavailable"); setApiError("The chat request could not reach the local API. Check the backend connection and CORS configuration."); }
    } finally { window.clearInterval(progressTimer); controllerRef.current = null; setBusy(false); setActiveRequestId(null); setRequestStage("idle"); }
  }

  async function stopRequest() {
    if (!activeRequestId) return;
    const requestId = activeRequestId;
    cancelledRef.current = true;
    try { await api(`/api/v1/requests/${activeRequestId}/cancel`, { method: "POST" }); } catch (error) { console.warn("cancel request failed", error); }
    controllerRef.current?.abort();
    setRequestStage("cancelled");
    const cancelled: Response = {
      thread_id: threadId || requestId, trace_id: requestId, disposition: "cancelled",
      answer_markdown: "Request cancelled. No result was committed to the conversation context.", citations: [],
      verification: { status: "not_applicable" }, execution_metadata: { request_id: requestId, terminal: "cancelled" },
    };
    setTurns((current) => [...current, { id: requestId, user: question.trim() || "(request)", assistant: cancelled, timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), trace: [] }]);
  }

  function newConversation() { setThreadId(undefined); setTurns([]); setTrace([]); setSelected(null); setSelectionCleared(false); setSnapshot(null); setSnapshotCleared(false); setSourcePreference("auto"); setDrawingEvidence(null); }
  useEffect(() => { timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "smooth" }); }, [turns, busy]);

  function adjustDrawingZoom(next: number) { setDrawingZoom(Math.max(.25, Math.min(5, next))); }
  function onDrawingWheel(event: React.WheelEvent<HTMLDivElement>) {
    // Chromium maps a macOS pinch gesture to ctrl+wheel. Keep that zoom local
    // to the drawing viewport, while ordinary two-finger motion pans its own
    // scroll container instead of the page.
    if (event.ctrlKey) { event.preventDefault(); adjustDrawingZoom(drawingZoom * (event.deltaY < 0 ? 1.1 : .9)); return; }
    event.stopPropagation();
  }
  function openCitation(citation: Citation) {
    if (citation.source_type === "ifc") { setTab("bim"); setSelected({ globalId: citation.locator.global_id, expressId: citation.locator.express_id, type: citation.locator.entity_type, name: citation.label }); }
    if (citation.source_type === "pdf") {
      setTab("drawing");
      setDrawingEvidence({ bbox: citation.locator.bbox, board: citation.locator.board, field: citation.locator.field, page: citation.locator.page });
      const bbox = citation.locator.bbox;
      const width = bbox && bbox.length >= 4 ? Math.max(1, bbox[2] - bbox[0]) : 700;
      const height = bbox && bbox.length >= 4 ? Math.max(1, bbox[3] - bbox[1]) : 500;
      const focusZoom = Math.max(1.35, Math.min(2.6, Math.min(1191 / (width * 1.25), 842 / (height * 1.25))));
      setDrawingZoom(focusZoom);
      window.setTimeout(() => drawingEvidenceRef.current?.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" }), 50);
    }
    if (citation.source_type === "viewer_snapshot") setTab("snapshot");
  }

  return <main>
    <header><div><p className="eyebrow">ARMIE · auditable multi-source project intelligence</p><h1>ARMIE Construction Intelligence Workbench</h1></div><span className="status">{status}</span></header>
    {apiState === "unavailable" && <p className="runtime-error" role="alert">{apiError}</p>}
    <div className="top-controls">
      <label>Source <select value={sourcePreference} onChange={(e) => setSourcePreference(e.target.value as SourcePreference)}><option value="auto">Auto</option><option value="ifc">IFC Model</option><option value="pdf">Engineering Drawing</option><option value="viewer_snapshot">Current Viewer Snapshot</option></select></label>
      <button type="button" onClick={newConversation}>New conversation</button><button type="button" onClick={() => { setSelected(null); setSelectionCleared(true); }}>Clear selection</button><button type="button" onClick={() => { setSnapshot(null); setSnapshotCleared(true); }}>Clear snapshot</button>
    </div>
    <section className="workspace">
      <aside className="viewer"><div className="tabs"><button className={tab === "bim" ? "active" : ""} onClick={() => setTab("bim")}>BIM Model</button><button className={tab === "drawing" ? "active" : ""} onClick={() => setTab("drawing")}>Drawing</button><button className={tab === "snapshot" ? "active" : ""} onClick={() => setTab("snapshot")}>Viewer Snapshot</button></div>
        {tab === "bim" && <><h2>IFC Viewer</h2><IfcViewer onSelection={handleSelection} onSnapshot={(value) => { setSnapshot(value); setSnapshotCleared(false); }} onStatus={setViewerStatus} focusGlobalId={selected?.globalId} /><dl className="selection-details"><div><dt>Element</dt><dd>{selected ? `${selected.type}: ${selected.name}` : "No IFC element selected"}</dd></div><div><dt>IFC type</dt><dd>{selected?.type || "—"}</dd></div><div><dt>ExpressID</dt><dd>{selected?.expressId ?? "—"}</dd></div><div><dt>GlobalId</dt><dd>{selected?.globalId || "—"}</dd></div></dl></>}
        {tab === "drawing" && <section className="drawing"><h2>Engineering Drawing</h2><p>{metadata?.pdf_file || "synthetic schedule"} · page {drawingEvidence?.page ?? 1}</p><div className="drawing-toolbar"><button type="button" onClick={() => adjustDrawingZoom(drawingZoom - .25)}>−</button><span>{Math.round(drawingZoom * 100)}%</span><button type="button" onClick={() => adjustDrawingZoom(drawingZoom + .25)}>+</button><button type="button" onClick={() => setDrawingZoom(1)}>Fit page</button><button type="button" onClick={() => setDrawingZoom(1.15)}>Fit width</button><button type="button" onClick={() => setDrawingZoom(1)}>100%</button>{drawingEvidence && <button type="button" onClick={() => { setDrawingEvidence(null); setDrawingZoom(1); }}>Clear evidence focus</button>}</div><div className="drawing-stage" aria-label="Zoomable engineering drawing. Pinch to zoom; two-finger scroll pans." onWheel={onDrawingWheel}><div className="drawing-page" style={{ transform: `scale(${drawingZoom})` }}><img src={`/api/v1/project/pdf/pages/${drawingEvidence?.page ?? 1}.png`} alt={`Engineering load schedule page ${drawingEvidence?.page ?? 1}`} />{drawingEvidence?.bbox && <div ref={drawingEvidenceRef} className="drawing-evidence-box" style={{ left: `${(drawingEvidence.bbox[0] / 1191) * 100}%`, top: `${(drawingEvidence.bbox[1] / 842) * 100}%`, width: `${((drawingEvidence.bbox[2] - drawingEvidence.bbox[0]) / 1191) * 100}%`, height: `${((drawingEvidence.bbox[3] - drawingEvidence.bbox[1]) / 842) * 100}%` }} title={`${drawingEvidence.board || "PDF evidence"}${drawingEvidence.field ? ` · ${drawingEvidence.field}` : ""}`} />}</div></div><p className="empty">{drawingEvidence ? `Focused evidence: ${drawingEvidence.board || "drawing region"}${drawingEvidence.field ? ` · ${drawingEvidence.field}` : ""}.` : "Pinch to zoom and use two-finger scrolling to pan; citations focus a cited drawing region."}</p></section>}
        {tab === "snapshot" && <section className="snapshot"><h2>Viewer Snapshot</h2>{snapshot ? <img src={snapshot} alt="Captured IFC viewer context" /> : <p className="empty">Capture a BIM view to enable image-grounded inspection.</p>}<p>Selected: {selected?.globalId || "none"}</p></section>}
      </aside>
      <section className="chat"><h2>Conversation</h2><div className="messages" ref={timelineRef}>{turns.length === 0 ? <p className="empty">Ask a BIM, drawing, or current-view question. Auto chooses the source; overrides remain in technical details.</p> : turns.map((turn) => <React.Fragment key={turn.id}><div className="message-row user"><article className="message user-message"><div className="message-meta"><span>User</span><time>{turn.timestamp}</time></div><p>{turn.user}</p></article></div><div className="message-row assistant"><article className={`message assistant-message ${turn.assistant.disposition}`}><div className="message-meta"><span>Assistant</span><span>{turn.assistant.disposition.replace(/_/g, " ")}</span><span className={turn.assistant.verification.status}>{turn.assistant.verification.status}</span><time>{turn.timestamp}</time></div><p>{turn.assistant.answer_markdown}</p>{turn.assistant.citations.length > 0 && <div className="turn-citations">{turn.assistant.citations.slice(0, 3).map((citation) => <button type="button" key={stableCitationKey(citation)} onClick={() => openCitation(citation)}>View {citation.source_type} evidence</button>)}</div>}<details className="technical-details"><summary>Technical details</summary><small>Source: {turn.assistant.execution_metadata.source || "—"} · Planner: {turn.assistant.execution_metadata.planning_mode || "—"} · Models: {turn.assistant.execution_metadata.model_call_count || 0} · Tools: {turn.assistant.execution_metadata.tool_call_count || 0} · Trace: {turn.assistant.trace_id}</small></details></article></div></React.Fragment>)}</div><form onSubmit={submit}><textarea value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. How many doors are in the project?" rows={3} /><div className="submit-row"><button disabled={busy}>{busy ? `Checking evidence… ${requestStage}` : "Ask with audit trail"}</button>{busy && <button type="button" className="stop-button" onClick={stopRequest}>Stop request</button>}</div></form></section>
      <aside className="inspector"><section className="citations"><h2>Evidence Inspector</h2>{citations.length === 0 ? <p className="empty">Evidence appears after a question.</p> : <><p className="empty">{latest?.citations.length} evidence items · {Math.min(citations.length, 3)} representative citations shown</p>{citations.slice(0, 3).map((citation) => <details key={stableCitationKey(citation)}><summary onClick={() => openCitation(citation)}>{citation.source_type}: {citation.label}</summary><dl className="citation-facts">{citationFacts(citation).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>{citation.locator.evidence_crop && <img className="evidence-crop" src={`/api/v1/evidence/${citation.locator.evidence_crop}`} alt="Cited PDF evidence crop" />}</details>)}</>}</section><section className="audit"><h2>Audit Trail</h2>{trace.length === 0 ? <p className="empty">Trace events appear after a question.</p> : <><div className="decision-summary"><strong>Decision Summary</strong><span>Intent: {latest?.execution_metadata.normalized_request || "—"}</span><span>Source: {latest?.execution_metadata.source || "—"}</span><span>Capability / Plan: {latest?.execution_metadata.subplans?.map((p: any) => `${p.operation || "—"}${p.entity_type ? ` · ${p.entity_type}` : ""}`).join(", ") || "—"}</span><span>Execution: {latest?.execution_metadata.tool_call_count || 0} tool · {latest?.execution_metadata.model_call_count || 0} model call(s)</span><span>Verification: {latest?.verification.status || "—"}</span><span>Evidence: {latest?.citations.length || 0} citation(s)</span><span>Result: {latest?.disposition.replace(/_/g, " ") || "—"}</span><span>Latency: {latest?.execution_metadata.latency_ms ? `${latest.execution_metadata.latency_ms} ms` : "—"}</span></div>{AUDIT_STAGES.map((stage) => { const events = trace.filter((event) => auditStage(event) === stage); return <details className="audit-stage" key={stage} open={stage === "Intent Understanding"}><summary>{stage} ({events.length})</summary>{events.length === 0 ? <p className="empty">No events.</p> : <ol>{events.map((event) => <li key={event.id}><strong>{event.summary}</strong>{event.actual_model && <small>Model: {event.actual_provider}/{event.actual_model}</small>}<details><summary>Details</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></li>)}</ol>}</details> })}<details className="raw-trace"><summary>Raw Trace ({trace.length} events)</summary><ol>{trace.map((event) => <li key={event.id}><strong>{event.step} · {event.event_type}</strong><span>{event.summary}</span>{event.actual_model && <small>Actual model: {event.actual_provider}/{event.actual_model}</small>}<details><summary>Payload</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></li>)}</ol></details></>}</section></aside>
    </section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
