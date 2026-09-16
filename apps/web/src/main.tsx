import React, { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, ApiAuthError, AuthedImage, ensureSessionId, setStoredApiKey, withAuthHeader } from "./apiClient";
import { DecisionStory } from "./DecisionStory";
import { Findings } from "./Findings";
import { IfcViewer, ViewerStatus } from "./IfcViewer";
import "./styles.css";

type SourcePreference = "auto" | "ifc" | "pdf" | "viewer_snapshot";
type WorkspaceTab = "bim" | "drawing" | "snapshot" | "findings";
type Citation = { evidence_id: string; source_type: string; label: string; locator: Record<string, any>; project_id?: string; source_set_id?: string; source_file?: string };
type TraceEvent = { id: string; step: string; event_type: string; summary: string; payload: Record<string, any>; actual_provider?: string; actual_model?: string; planning_mode?: string; model_call_count?: number; tool_call_count?: number };
type ReconciliationItem = { tag: string; entity_type?: string | null; storey?: string | null; ifc_width_m?: number | null; ifc_height_m?: number | null; pdf_width_m?: number | null; pdf_height_m?: number | null; status: string; detail: string };
type Response = {
  thread_id: string; trace_id: string; disposition: "answered" | "partially_answered" | "clarification_required" | "refused" | "error" | "timeout" | "cancelled";
  answer_markdown: string; citations: Citation[]; verification: { status: string; reason?: string };
  execution_metadata: Record<string, any>; reconciliation_items?: ReconciliationItem[];
};
type Selected = { globalId?: string; expressId?: number; type?: string; name?: string };
type ConversationTurn = { id: string; user: string; assistant: Response; timestamp: string; trace: TraceEvent[] };

// `_natural_answer` (apps/api/app/agent/graph.py) intentionally wraps
// numbers/entities in literal `**bold**` markdown so the emphasis survives
// as plain text if nothing renders it -- but nothing here ever did, so the
// literal asterisks showed up in the chat bubble instead of bold text
// (found live, 2026-09-13). This is deliberately not a full markdown
// renderer: the backend only ever emits this one non-nested construct.
function renderAnswerMarkdown(text: string): React.ReactNode {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4
      ? <strong key={index}>{part.slice(2, -2)}</strong>
      : <React.Fragment key={index}>{part}</React.Fragment>
  );
}

// A cited bbox is tight to the actual text ink (word-level extraction, no
// margin) -- rendering the evidence-focus outline exactly at that
// boundary put the 3px outline itself directly across the glyphs at
// higher zoom levels, obscuring the very number it was meant to point at.
// Found live, 2026-09-14. Padded by a fixed few PDF points on every side
// (in the bbox's own coordinate space, before converting to percentages)
// so the highlighted region visibly surrounds the value instead of
// hugging it.
const EVIDENCE_BOX_PADDING_PT = 4;

function evidenceBoxStyle(bbox: number[], pageWidth: number, pageHeight: number): React.CSSProperties {
  const [x0, y0, x1, y1] = bbox;
  const left = Math.max(0, x0 - EVIDENCE_BOX_PADDING_PT);
  const top = Math.max(0, y0 - EVIDENCE_BOX_PADDING_PT);
  const right = Math.min(pageWidth, x1 + EVIDENCE_BOX_PADDING_PT);
  const bottom = Math.min(pageHeight, y1 + EVIDENCE_BOX_PADDING_PT);
  return {
    left: `${(left / pageWidth) * 100}%`,
    top: `${(top / pageHeight) * 100}%`,
    width: `${((right - left) / pageWidth) * 100}%`,
    height: `${((bottom - top) / pageHeight) * 100}%`,
  };
}

type ProjectOption = { project_id: string; display_name: string };

function App() {
  const [metadata, setMetadata] = useState<Record<string, any> | null>(null);
  // SPEC-M9: undefined means the backend's own "demo" default (every
  // deployment before this milestone, and any deployment that leaves
  // ADLS unset, has exactly this one implicit project). `projects` stays
  // empty in that mode too, so the selector below never renders.
  const [projectId, setProjectId] = useState<string | undefined>(undefined);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  // Bumped on every project switch; a request's response is discarded
  // (never applied to conversation/viewer/evidence state) if this counter
  // has moved on by the time it resolves -- closes the "a switch happens
  // while project A's request is still in flight, and its late response
  // corrupts project B's now-active view" race an independent review of
  // this milestone's spec raised.
  const switchSeqRef = useRef(0);
  const [threadId, setThreadId] = useState<string | undefined>();
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [selectionCleared, setSelectionCleared] = useState(false);
  const [snapshot, setSnapshot] = useState<string | null>(null);
  const [snapshotCleared, setSnapshotCleared] = useState(false);
  const [sourcePreference, setSourcePreference] = useState<SourcePreference>("auto");
  // SPEC-M16: "v1" (default) is today's engine, completely unaffected by
  // this toggle -- submit() below is untouched. "v2" is the new,
  // independent tool-calling agent (SS C), submitted via the separate
  // submitV2() path so V1's own request/response handling never has to
  // account for a second response shape (a streamed SSE turn instead of
  // one JSON object).
  const [engine, setEngine] = useState<"v1" | "v2">("v1");
  const [v2Streaming, setV2Streaming] = useState<{ statuses: string[]; answer: string } | null>(null);
  const [tab, setTab] = useState<WorkspaceTab>("bim");
  const [drawingZoom, setDrawingZoom] = useState(1);
  const [drawingEvidence, setDrawingEvidence] = useState<{ bbox?: number[]; board?: string; field?: string; page?: number; document?: string; localized?: boolean } | null>(null);
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
  const [needsApiKey, setNeedsApiKey] = useState(false);
  const [apiKeyInput, setApiKeyInput] = useState("");

  const loadMetadata = useCallback(() => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    api<Record<string, any>>(`/api/v1/project/metadata${query}`).then((value) => {
      setMetadata(value); setApiState("ready"); setNeedsApiKey(false); void ensureSessionId();
      // Fetched here, not from its own mount-time effect: this app has no
      // gate component wrapping it, so a mount-time effect with an empty
      // dependency array fires immediately, before the access-key gate
      // below has resolved -- sessionStorage has no key yet on a fresh
      // tab, so that fetch always 401ed, permanently latched `projects`
      // to [] for the rest of the session even after a correct key was
      // submitted (submitApiKey only re-runs loadMetadata, never that
      // separate effect). Found live: the deployed multi-project selector
      // never appeared at all, on every fresh session, regardless of
      // whether ADLS mode was actually on. Fetching alongside metadata
      // guarantees this only runs once the API key is already known good.
      api<ProjectOption[]>("/api/v1/projects").then(setProjects).catch(() => setProjects([]));
    })
      .catch((error: Error) => {
        if (error instanceof ApiAuthError) { setNeedsApiKey(true); return; }
        console.error(error); setApiState("unavailable"); setApiError("The local API is unavailable. Start FastAPI on port 8000 and reload.");
      });
  }, [projectId]);

  const handleSelection = useCallback((element: Selected | null) => { setSelected(element); setSelectionCleared(false); }, []);
  // D-049: owner-requested, 2026-09-15 -- several evidence citations (or
  // directly-clicked elements) should be able to stay highlighted in the
  // 3D view at once, each turned on/off independently by the owner,
  // rather than one highlight always replacing whatever was lit before
  // (D-046) or vanishing the moment the camera rotates. Ownership lives
  // here, shared between IfcViewer's own canvas clicks and DecisionStory's
  // citation "Jump to this evidence" buttons, rather than each tracking
  // its own separate notion of "what's highlighted."
  const [highlightedIds, setHighlightedIds] = useState<Set<string>>(new Set());
  const toggleHighlight = useCallback((globalId: string) => {
    setHighlightedIds((current) => {
      const next = new Set(current);
      if (next.has(globalId)) next.delete(globalId); else next.add(globalId);
      return next;
    });
  }, []);
  useEffect(() => { loadMetadata(); }, [loadMetadata]);

  function submitApiKey(event: FormEvent) {
    event.preventDefault();
    if (!apiKeyInput.trim()) return;
    setStoredApiKey(apiKeyInput.trim());
    setApiKeyInput("");
    loadMetadata();
  }

  const status = useMemo(() => {
    if (apiState === "loading") return "Loading application…";
    if (apiState === "unavailable") return "API unavailable";
    if (!metadata?.ifc_available) return "IFC source unavailable";
    return viewerStatus.phase === "ready" ? "Viewer ready" : viewerStatus.message;
  }, [apiState, metadata, viewerStatus]);
  const latest = turns.length ? turns[turns.length - 1].assistant : undefined;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || busy) return;
    const requestId = crypto.randomUUID();
    const switchSeqAtStart = switchSeqRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    cancelledRef.current = false;
    setActiveRequestId(requestId); setRequestStage("queued"); setBusy(true);
    const progressTimer = window.setInterval(() => {
      api<{ stage?: string }>(`/api/v1/requests/${requestId}`).then((state) => setRequestStage(state.stage || "running")).catch(() => undefined);
    }, 600);
    try {
      const response = await api<Response>("/api/v1/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        request_id: requestId, thread_id: threadId, project_id: projectId, question, source_preference: sourcePreference,
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
      // A project switch happened while this request was still in flight
      // -- discard the response rather than let it corrupt the
      // now-active project's conversation/viewer/evidence state.
      if (switchSeqRef.current !== switchSeqAtStart) return;
      setThreadId(response.thread_id); setTurns((current) => [...current, { id: response.trace_id, user: question.trim(), assistant: response, timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), trace: responseTrace }]); setQuestion("");
      setTrace(responseTrace);
    } catch (error) {
      if (error instanceof ApiAuthError) { setNeedsApiKey(true); }
      else if ((error as Error).name !== "AbortError") { console.error(error); setApiState("unavailable"); setApiError("The chat request could not reach the local API. Check the backend connection and CORS configuration."); }
    } finally { window.clearInterval(progressTimer); controllerRef.current = null; setBusy(false); setActiveRequestId(null); setRequestStage("idle"); }
  }

  // SPEC-M16: V2's own submit path -- a separate function, not a branch
  // inside submit() above, so V1's request/response handling (a single
  // JSON object) never has to account for V2's streamed SSE shape.
  // Consumes the response body as a raw stream (fetch + ReadableStream,
  // not EventSource, which cannot POST or carry the auth header this app
  // needs) and renders tool-use status lines and answer tokens as they
  // arrive, then folds the final event into the same `turns` list V1
  // uses, labeled by engine so a benchmark comparison is legible without
  // cross-referencing the audit trail.
  async function submitV2(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || busy) return;
    const askedQuestion = question.trim();
    setBusy(true);
    setV2Streaming({ statuses: [], answer: "" });
    setQuestion("");
    try {
      const response = await fetch("/api/v1/chat", withAuthHeader({
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ thread_id: threadId, project_id: projectId, question: askedQuestion, engine: "v2" }),
      }));
      if (!response.ok || !response.body) throw new Error(`V2 request failed (${response.status}).`);
      const newThreadId = response.headers.get("X-Thread-Id") || threadId;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finalResponse: Response | null = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const event = JSON.parse(line.slice("data: ".length));
          if (event.type === "thinking") {
            // SPEC-M16 SS E: the tool-selection round cannot itself be
            // streamed (it must be fully received before its arguments
            // are parseable) -- real measurement found this is most of a
            // turn's wall time. An immediate "thinking" line is the
            // honest mitigation: something visible right away, not
            // several seconds of silence before the first real status.
            setV2Streaming((current) => current && { ...current, statuses: ["Thinking…"] });
          } else if (event.type === "tool_status") {
            setV2Streaming((current) => current && {
              ...current,
              statuses: event.status === "started"
                ? [...current.statuses.filter((s) => s !== "Thinking…"), `Calling ${event.tool_name}…`]
                : current.statuses.map((s) => s === `Calling ${event.tool_name}…` ? `${event.tool_name} ✓` : s),
            });
          } else if (event.type === "answer_chunk") {
            setV2Streaming((current) => current && { ...current, statuses: current.statuses.filter((s) => s !== "Thinking…"), answer: current.answer + event.text });
          } else if (event.type === "final") {
            finalResponse = event.response as Response;
          } else if (event.type === "error") {
            throw new Error(event.message);
          }
        }
      }
      if (finalResponse) {
        setThreadId(newThreadId || undefined);
        setTurns((current) => [...current, {
          id: finalResponse!.trace_id, user: askedQuestion, assistant: finalResponse!,
          timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), trace: [],
        }]);
      }
    } catch (error) {
      console.error(error);
      setApiError(`The V2 agent request failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
      setV2Streaming(null);
    }
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

  // question was never cleared here, found live while stress-testing
  // project switching: submit()'s discard-on-switch branch
  // (switchSeqRef.current !== switchSeqAtStart -- see switchProject
  // below) returns before its own setQuestion(""), so a question
  // in flight when the user switches projects survived into the new
  // conversation; typing a fresh question then silently concatenated
  // onto the old, abandoned one with no separator, since both landed in
  // the same textarea. newConversation() is the actual "start clean"
  // action (called directly, and by switchProject below), so it -- not
  // submit()'s own success path -- is the right place to guarantee this.
  function newConversation() { setThreadId(undefined); setTurns([]); setTrace([]); setSelected(null); setSelectionCleared(false); setSnapshot(null); setSnapshotCleared(false); setSourcePreference("auto"); setDrawingEvidence(null); setQuestion(""); }

  function switchProject(nextProjectId: string) {
    // OD-40: switching projects implicitly starts a new conversation (the
    // backend also enforces one project per thread -- this is belt and
    // suspenders, not the only guard). Cancel any in-flight request and
    // bump the switch sequence first, so a response that was already on
    // its way back from the old project cannot land in the new one.
    switchSeqRef.current += 1;
    cancelledRef.current = true;
    controllerRef.current?.abort();
    setProjectId(nextProjectId || undefined);
    newConversation();
  }
  useEffect(() => { timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "smooth" }); }, [turns, busy]);

  function adjustDrawingZoom(next: number) { setDrawingZoom(Math.max(.25, Math.min(5, next))); }
  function onDrawingWheel(event: React.WheelEvent<HTMLDivElement>) {
    // Chromium maps a macOS pinch gesture to ctrl+wheel. Keep that zoom local
    // to the drawing viewport, while ordinary two-finger motion pans its own
    // scroll container instead of the page.
    if (event.ctrlKey) { event.preventDefault(); adjustDrawingZoom(drawingZoom * (event.deltaY < 0 ? 1.1 : .9)); return; }
    event.stopPropagation();
  }
  // SPEC-M6 deliberately scoped the drawing viewer to the first-configured
  // document only; the answer pipeline (_execute_pdf_multi_document) has
  // been multi-document-aware since that milestone, but nothing let a user
  // actually look at the other configured documents. Found live,
  // 2026-09-13: "there are multiple documents but I can only ever see page
  // 1 of the first one." `drawingDocument` defaults to the citation's own
  // source_file when evidence is focused, else the project's first
  // configured document, so switching documents/pages doesn't require a
  // citation to already be open.
  const drawingDocument = drawingEvidence?.document || metadata?.pdf_files?.[0];
  const drawingDocInfo = (metadata?.pdf_documents || []).find((doc: { filename: string; page_count: number }) => doc.filename === drawingDocument);
  const drawingPageCount = drawingDocInfo?.page_count || 1;

  function changeDrawingDocument(document: string) {
    setDrawingEvidence({ document, page: 1 });
    setDrawingZoom(1);
  }
  function changeDrawingPage(page: number) {
    setDrawingEvidence((current) => ({ ...(current || {}), document: current?.document || drawingDocument, page: Math.max(1, Math.min(drawingPageCount, page)), bbox: undefined }));
  }

  function openCitation(citation: Citation) {
    // D-049: owner-requested, 2026-09-15 -- each citation's highlight now
    // toggles independently (via the shared `highlightedIds` set above)
    // instead of one citation's focus replacing another's (D-046) --
    // several can be lit in the 3D view at once. The "Element" info panel
    // below still always describes whichever citation was clicked most
    // recently, regardless of which direction its highlight just toggled.
    if (citation.source_type === "ifc") {
      setTab("bim");
      setSelected({ globalId: citation.locator.global_id, expressId: citation.locator.express_id, type: citation.locator.entity_type, name: citation.label });
      if (citation.locator.global_id) toggleHighlight(citation.locator.global_id);
    }
    if (citation.source_type === "pdf") {
      setTab("drawing");
      setDrawingEvidence({ bbox: citation.locator.bbox, board: citation.locator.board, field: citation.locator.field, page: citation.locator.page, document: citation.source_file, localized: citation.locator.localized });
      const bbox = citation.locator.bbox;
      const width = bbox && bbox.length >= 4 ? Math.max(1, bbox[2] - bbox[0]) : 700;
      const height = bbox && bbox.length >= 4 ? Math.max(1, bbox[3] - bbox[1]) : 500;
      const pageSize = (metadata?.pdf_documents || []).find((doc: { filename: string }) => doc.filename === citation.source_file)?.page_sizes?.[(citation.locator.page || 1) - 1] || [1191, 842];
      const focusZoom = Math.max(1.35, Math.min(2.6, Math.min(pageSize[0] / (width * 1.25), pageSize[1] / (height * 1.25))));
      setDrawingZoom(focusZoom);
      window.setTimeout(() => drawingEvidenceRef.current?.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" }), 50);
    }
    if (citation.source_type === "viewer_snapshot") setTab("snapshot");
  }

  if (needsApiKey) {
    return <main className="api-key-gate">
      <form onSubmit={submitApiKey}>
        <h1>Access key required</h1>
        <p>This workbench requires a shared access key (SPEC-M5). Ask the project owner for it.</p>
        <input type="password" value={apiKeyInput} onChange={(e) => setApiKeyInput(e.target.value)} placeholder="Access key" autoFocus />
        <button type="submit">Continue</button>
      </form>
    </main>;
  }

  return <main>
    <header><div><p className="eyebrow">ARMIE · auditable multi-source project intelligence</p><h1>ARMIE Construction Intelligence Workbench</h1></div><span className="status">{status}</span></header>
    {apiState === "unavailable" && <p className="runtime-error" role="alert">{apiError}</p>}
    <div className="top-controls">
      {projects.length > 0 && <label>Project <select value={projectId || "demo"} onChange={(e) => switchProject(e.target.value)}>{projects.map((p) => <option key={p.project_id} value={p.project_id}>{p.display_name}</option>)}</select></label>}
      <label>Source <select value={sourcePreference} onChange={(e) => setSourcePreference(e.target.value as SourcePreference)}><option value="auto">Auto</option><option value="ifc">IFC Model</option><option value="pdf">Engineering Drawing</option><option value="viewer_snapshot">Current Viewer Snapshot</option></select></label>
      {/* SPEC-M16: explicit, visible per-question engine choice -- never
          automatic/silent routing between V1 and V2 (this spec's own
          Invariant). Defaults to V1 every time the app loads. */}
      <label title="V1: today's engine. V2: the new independent tool-calling agent (beta) -- pick either per question to compare them.">Engine <select value={engine} onChange={(e) => setEngine(e.target.value as "v1" | "v2")}><option value="v1">V1 (current)</option><option value="v2">V2 (agent, beta)</option></select></label>
      <button type="button" onClick={newConversation}>New conversation</button><button type="button" onClick={() => { setSelected(null); setSelectionCleared(true); setHighlightedIds(new Set()); }}>Clear selection</button><button type="button" onClick={() => { setSnapshot(null); setSnapshotCleared(true); }}>Clear snapshot</button>
    </div>
    <section className="workspace">
      <aside className="viewer"><div className="tabs"><button className={tab === "bim" ? "active" : ""} onClick={() => setTab("bim")}>BIM Model</button><button className={tab === "drawing" ? "active" : ""} onClick={() => setTab("drawing")}>Drawing</button><button className={tab === "snapshot" ? "active" : ""} onClick={() => setTab("snapshot")}>Viewer Snapshot</button><button className={tab === "findings" ? "active" : ""} onClick={() => setTab("findings")}>Findings</button></div>
        {tab === "bim" && <><h2>IFC Viewer</h2><IfcViewer projectId={projectId} onSelection={handleSelection} onSnapshot={(value) => { setSnapshot(value); setSnapshotCleared(false); }} onStatus={setViewerStatus} highlightedGlobalIds={highlightedIds} onToggleHighlight={toggleHighlight} /><dl className="selection-details"><div><dt>Element</dt><dd>{selected ? `${selected.type}: ${selected.name}` : "No IFC element selected"}</dd></div><div><dt>IFC type</dt><dd>{selected?.type || "—"}</dd></div><div><dt>ExpressID</dt><dd>{selected?.expressId ?? "—"}</dd></div><div><dt>GlobalId</dt><dd>{selected?.globalId || "—"}</dd></div></dl></>}
        {tab === "drawing" && <section className="drawing"><h2>Engineering Drawing</h2>
          <div className="drawing-toolbar">
            {(metadata?.pdf_files?.length || 0) > 1 && <label>Document <select value={drawingDocument || ""} onChange={(e) => changeDrawingDocument(e.target.value)}>{metadata!.pdf_files.map((name: string) => <option key={name} value={name}>{name}</option>)}</select></label>}
            <button type="button" onClick={() => changeDrawingPage((drawingEvidence?.page ?? 1) - 1)} disabled={(drawingEvidence?.page ?? 1) <= 1}>‹ Prev page</button>
            <span>Page {drawingEvidence?.page ?? 1} of {drawingPageCount}</span>
            <button type="button" onClick={() => changeDrawingPage((drawingEvidence?.page ?? 1) + 1)} disabled={(drawingEvidence?.page ?? 1) >= drawingPageCount}>Next page ›</button>
            <button type="button" onClick={() => adjustDrawingZoom(drawingZoom - .25)}>−</button><span>{Math.round(drawingZoom * 100)}%</span><button type="button" onClick={() => adjustDrawingZoom(drawingZoom + .25)}>+</button><button type="button" onClick={() => setDrawingZoom(1)}>Fit page</button><button type="button" onClick={() => setDrawingZoom(1.15)}>Fit width</button>{drawingEvidence && <button type="button" onClick={() => { setDrawingEvidence(null); setDrawingZoom(1); }}>Clear evidence focus</button>}
          </div>
          <div className="drawing-stage" aria-label="Zoomable engineering drawing. Pinch to zoom; two-finger scroll pans." onWheel={onDrawingWheel}><div className="drawing-page" style={{ transform: `scale(${drawingZoom})` }}><AuthedImage src={`/api/v1/project/pdf/pages/${drawingEvidence?.page ?? 1}.png?${new URLSearchParams({ ...(projectId ? { project_id: projectId } : {}), ...(drawingDocument ? { document: drawingDocument } : {}) }).toString()}`} alt={`${drawingDocument || "Engineering load schedule"} page ${drawingEvidence?.page ?? 1}`} />{drawingEvidence?.bbox && <div ref={drawingEvidenceRef} className="drawing-evidence-box" style={evidenceBoxStyle(drawingEvidence.bbox, drawingDocInfo?.page_sizes?.[(drawingEvidence.page || 1) - 1]?.[0] || 1191, drawingDocInfo?.page_sizes?.[(drawingEvidence.page || 1) - 1]?.[1] || 842)} title={`${drawingEvidence.board || "PDF evidence"}${drawingEvidence.field ? ` · ${drawingEvidence.field}` : ""}`} />}</div></div><p className="empty">{drawingEvidence?.localized === false ? "This evidence's location could not be precisely determined; showing the full page for manual review." : drawingEvidence?.bbox ? `Focused evidence: ${drawingEvidence.board || "drawing region"}${drawingEvidence.field ? ` · ${drawingEvidence.field}` : ""}.` : "Pinch to zoom and use two-finger scrolling to pan; citations focus a cited drawing region."}</p></section>}
        {tab === "snapshot" && <section className="snapshot"><h2>Viewer Snapshot</h2>{snapshot ? <img src={snapshot} alt="Captured IFC viewer context" /> : <p className="empty">Capture a BIM view to enable image-grounded inspection.</p>}<p>Selected: {selected?.globalId || "none"}</p></section>}
        {tab === "findings" && <Findings projectId={projectId} />}
      </aside>
      <section className="chat"><h2>Conversation</h2><div className="messages" ref={timelineRef}>{turns.length === 0 ? <p className="empty">Ask a BIM, drawing, or current-view question. Auto chooses the source; overrides remain in technical details.</p> : turns.map((turn) => <React.Fragment key={turn.id}><div className="message-row user"><article className="message user-message"><div className="message-meta"><span>User</span><time>{turn.timestamp}</time></div><p>{turn.user}</p></article></div><div className="message-row assistant"><article className={`message assistant-message ${turn.assistant.disposition}`}><div className="message-meta"><span>Assistant</span>{/* SPEC-M16: labeled so a V1/V2 benchmark comparison is legible without cross-referencing the audit trail. */}<span className="engine-badge">{turn.assistant.execution_metadata.engine === "v2" ? "V2 (agent)" : "V1"}</span><span>{turn.assistant.disposition.replace(/_/g, " ")}</span><span className={turn.assistant.verification.status}>{turn.assistant.verification.status}</span><time>{turn.timestamp}</time></div><p>{renderAnswerMarkdown(turn.assistant.answer_markdown)}</p><details className="technical-details"><summary>Technical details</summary><small>Source: {turn.assistant.execution_metadata.source || "—"} · Planner: {turn.assistant.execution_metadata.planning_mode || "—"} · Models: {turn.assistant.execution_metadata.model_call_count || 0} · Tools: {turn.assistant.execution_metadata.tool_call_count || 0} · Latency: {turn.assistant.execution_metadata.latency_ms ? `${turn.assistant.execution_metadata.latency_ms} ms` : "—"} · Trace: {turn.assistant.trace_id}</small></details></article></div></React.Fragment>)}
        {/* SPEC-M16 SS F: V2's own live progress -- tool-use status lines
            clear/tick as calls resolve, then the final answer's tokens
            append as they stream in, verified live in the browser (this
            session's own established standard), not just unit-tested. */}
        {v2Streaming && <div className="message-row assistant"><article className="message assistant-message v2-streaming"><div className="message-meta"><span>Assistant</span><span className="engine-badge">V2 (agent)</span><span>working…</span></div>{v2Streaming.statuses.map((status, index) => <p key={index} className="v2-tool-status">{status}</p>)}{v2Streaming.answer && <p>{renderAnswerMarkdown(v2Streaming.answer)}</p>}</article></div>}
      </div><form onSubmit={engine === "v2" ? submitV2 : submit}><textarea value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. How many doors are in the project?" rows={3} /><div className="submit-row"><button disabled={busy}>{busy ? (engine === "v2" ? "Agent working…" : `Checking evidence… ${requestStage}`) : "Ask with audit trail"}</button>{busy && engine === "v1" && <button type="button" className="stop-button" onClick={stopRequest}>Stop request</button>}</div></form></section>
      <aside className="inspector"><DecisionStory latest={latest} trace={trace} projectId={projectId} onOpenCitation={openCitation} /></aside>
    </section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
