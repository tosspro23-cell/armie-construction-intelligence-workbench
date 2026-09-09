import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.agent.graph import AgentService
from app.config import get_settings
from app.persistence.conversation_store import ConversationStore
from app.schemas.models import (
    AgentResponse,
    AuditEvent,
    ChatRequest,
    ClarificationResumeRequest,
    Disposition,
    VerificationStatus,
)
from app.services import ServiceContainer
from app.telemetry import configure_telemetry


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.container = ServiceContainer(settings)
    app.state.agent = AgentService(app.state.container)
    # app.state.requests is intentionally never persisted (SPEC-M4 §Excluded
    # scope): each record holds a live asyncio.Task used for cancellation,
    # which has no meaningful representation outside the event loop that
    # created it. Conversation context and audit events, in contrast, move
    # to ServiceContainer.conversation_store/.audit_store (SPEC-M4 §A/B),
    # which durably persist across a process restart when configured with
    # DATABASE_URL.
    app.state.requests = {}
    yield
    # Independent-review addition: PostgresConversationStore/PostgresAuditStore
    # (SPEC-M4) hold open psycopg connection pools; InMemoryConversationStore/
    # JsonlAuditStore hold none, so they don't define close() at all -- this
    # stays duck-typed rather than adding a no-op close() to the
    # ConversationStore/AuditStore Protocols, so those interfaces stay
    # exactly the two methods D-013 documents them as.
    for store in (app.state.container.conversation_store, app.state.container.audit_store):
        close = getattr(store, "close", None)
        if close is not None:
            close()


app = FastAPI(
    title="ARMIE Construction Intelligence Workbench",
    version="0.1.0",
    lifespan=lifespan,
)
# Must run here, right after construction -- not from inside `lifespan` --
# or FastAPIInstrumentor's added middleware never takes effect. Independent
# review finding, confirmed with an isolated reproduction (TestClient +
# InMemorySpanExporter, no network): instrumenting from inside `lifespan`
# exported zero spans for a real request; instrumenting here exported the
# expected SERVER span. This was the actual root cause of AppRequests
# showing zero rows in Application Insights despite AppDependencies/
# AppMetrics receiving data correctly (previously an open, undiagnosed
# gap -- see D-012/PROJECT_STATE.md's M3 entry).
configure_telemetry(app, get_settings())
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def health() -> dict:
    container: ServiceContainer = app.state.container
    return {"status": "ok", "project": container.project_metadata()}


@app.get("/api/v1/project/metadata")
def project_metadata() -> dict:
    container: ServiceContainer = app.state.container
    metadata = container.project_metadata()
    if metadata["ifc_available"]:
        metadata["ifc"] = container.ifc_repository.metadata()
    if metadata["pdf_available"]:
        metadata["pdf"] = container.document_analyzer.inspect()
    return metadata


@app.get("/api/v1/project/ifc")
def project_ifc():
    """Serve the configured local IFC only to the local viewer workflow."""
    path = app.state.container.settings.ifc_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Configured IFC source file was not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@app.get("/api/v1/project/pdf")
def project_pdf():
    path = app.state.container.settings.pdf_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Configured PDF source file was not found.")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/api/v1/project/pdf/pages/{page_number}.png")
def project_pdf_page(page_number: int):
    analyzer = app.state.container.document_analyzer
    if page_number < 1 or not analyzer.available:
        raise HTTPException(status_code=404, detail="Requested PDF page was not found.")
    return FileResponse(analyzer.render_page(page_number, scale=1.5), media_type="image/png")


@app.get("/api/v1/evidence/{filename}")
def evidence_file(filename: str):
    # Basename containment prevents this local-review endpoint from exposing
    # arbitrary paths while allowing PDF crops and rendered pages in the UI.
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid evidence file name.")
    path = app.state.container.settings.evidence_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Evidence file was not found.")
    return FileResponse(path)


@app.get("/api/v1/project/viewer-elements")
def project_viewer_elements() -> dict:
    """Expose a lightweight projection of the real local IFC for the browser viewer."""
    repository = app.state.container.ifc_repository
    if not repository.available:
        raise HTTPException(status_code=404, detail="Configured IFC source file was not found.")
    return {
        "source_file": repository.path.name,
        "representation": "ifcopenshell_bounding_geometry",
        "elements": repository.viewer_elements,
    }


def _terminal_response(request: ChatRequest, request_id: str, disposition: Disposition, message: str, **extra_metadata) -> AgentResponse:
    thread_id = request.thread_id or request_id
    return AgentResponse(
        thread_id=thread_id, trace_id=request_id, disposition=disposition, answer_markdown=message,
        verification=VerificationStatus(status="not_applicable", reason=message),
        execution_metadata={"request_id": request_id, "terminal": disposition.value, **extra_metadata},
    )


async def _safe_audit_append(audit_store, event: AuditEvent) -> str | None:
    """Best-effort audit write for a terminal branch (cancelled/timeout):
    the response's disposition is already fully determined by this point,
    so a persistence failure here (e.g. Postgres unreachable, SPEC-M4) must
    not turn an honest cancelled/timeout response into an unhandled 500.
    Returns an error string (surfaced in execution_metadata, never silently
    dropped) on failure, None on success.
    """
    try:
        await asyncio.to_thread(audit_store.append, event)
        return None
    except Exception as error:
        return str(error)


@app.post("/api/v1/chat")
async def chat(request: ChatRequest):
    agent: AgentService = app.state.agent
    conversations: ConversationStore = app.state.container.conversation_store
    request_id = request.request_id or str(uuid4())
    record = {"status": "running", "stage": "queued", "task": asyncio.current_task(), "trace_id": request_id}
    app.state.requests[request_id] = record
    try:
        # await asyncio.to_thread, not a direct call: with SPEC-M4's
        # Postgres-backed ConversationStore configured, this is a real
        # network round trip -- a direct synchronous call here would block
        # this process's single event loop (and therefore every other
        # concurrent request, health check, and cancellation) for as long
        # as the database takes to respond. Also inside this try, unlike
        # before: an unreachable/erroring store must produce this
        # endpoint's normal honest `error` disposition (D-004/D-010), not
        # an unhandled exception that bypasses it entirely -- confirmed by
        # fault injection in tests/test_chat_persistence_failure_handling.py.
        context = await asyncio.to_thread(conversations.get, request.thread_id or "") or {}
    except Exception as error:
        record.update(status="error", stage="context_read_error", error=str(error))
        return _terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: could not read conversation context: {error}")
    record["stage"] = "agent_execution"
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(
            agent.invoke,
            thread_id=request.thread_id,
            question=request.question,
            viewer_context=request.viewer_context.model_dump() if request.viewer_context else None,
            conversation_context=context,
            source_preference=request.source_preference,
        ), timeout=app.state.container.settings.request_timeout_seconds)
    except asyncio.CancelledError:
        record.update(status="cancelled", stage="cancelled")
        audit_error = await _safe_audit_append(app.state.container.audit_store, AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="cancelled", summary="Request was cancelled before a terminal response.", payload={"request_id": request_id}))
        extra = {"audit_persist_error": audit_error} if audit_error else {}
        return _terminal_response(request, request_id, Disposition.CANCELLED, "Request cancelled. No result was committed to the conversation context.", **extra)
    except asyncio.TimeoutError:
        record.update(status="timeout", stage="timeout")
        audit_error = await _safe_audit_append(app.state.container.audit_store, AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="timeout", summary="Request exceeded the bounded request deadline.", payload={"request_id": request_id, "timeout_seconds": app.state.container.settings.request_timeout_seconds}))
        extra = {"audit_persist_error": audit_error} if audit_error else {}
        return _terminal_response(request, request_id, Disposition.TIMEOUT, "The request exceeded its time limit. Partial audit events were preserved; please retry with a narrower question.", **extra)
    except Exception as error:
        record.update(status="error", stage="error", error=str(error))
        return _terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: {error}")

    response = response.model_copy(update={
        "execution_metadata": {
            **response.execution_metadata,
            "request_id": request_id,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    })
    context_persist_error: str | None = None
    if response.disposition.value in {"answered", "clarification_required"}:
        # A conversation-context persistence failure here must not discard
        # an already-correctly-computed, already-verified answer (the
        # response object above is complete and valid) -- losing
        # conversational continuity for the *next* turn is a strictly
        # lesser failure than a 500 on this one. Surfaced via
        # context_persist_error in execution_metadata rather than silently
        # dropped, so it is at least observable.
        try:
            context_update = response.context_update
            if context_update:
                await asyncio.to_thread(conversations.set, response.thread_id, context_update)
            else:
                trace = await asyncio.to_thread(app.state.container.audit_store.by_trace, response.trace_id)
                latest_plan = next((event.payload.get("plan") for event in reversed(trace) if event.event_type == "route_selected"), None)
                await asyncio.to_thread(conversations.set, response.thread_id, {
                    "previous_query_plan": latest_plan,
                    "active_entity_type": latest_plan.get("entity_type") if latest_plan else None,
                    "active_filters": latest_plan.get("filters", {}) if latest_plan else {},
                    "active_group_by": latest_plan.get("group_by") if latest_plan else None,
                    "evidence_refs": [citation.evidence_id for citation in response.citations],
                })
        except Exception as error:
            context_persist_error = str(error)
    # Recorded as the terminal state only after every persistence attempt
    # above has actually completed (successfully or not) -- previously this
    # was set before those calls, so a persistence failure could leave
    # app.state.requests reporting "completed" while the client-facing call
    # was still in flight or about to raise.
    record.update(status="completed", stage="completed", trace_id=response.trace_id)
    if context_persist_error:
        response = response.model_copy(update={
            "execution_metadata": {**response.execution_metadata, "context_persist_error": context_persist_error}
        })
    return response


@app.post("/api/v1/chat/{thread_id}/resume")
async def resume(thread_id: str, request: ClarificationResumeRequest):
    conversations: ConversationStore = app.state.container.conversation_store
    try:
        existing = await asyncio.to_thread(conversations.get, thread_id)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Could not read conversation state: {error}") from error
    if existing is None:
        raise HTTPException(status_code=404, detail="No resumable conversation was found for this thread.")
    return await chat(ChatRequest(thread_id=thread_id, question=request.answer))


@app.post("/api/v1/requests/{request_id}/cancel")
async def cancel_request(request_id: str):
    record = app.state.requests.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request was not found.")
    record["status"] = "cancel_requested"
    task = record.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    return {"request_id": request_id, "status": "cancel_requested"}


@app.get("/api/v1/requests/{request_id}")
def request_status(request_id: str):
    record = app.state.requests.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request was not found.")
    return {key: value for key, value in record.items() if key != "task"}


@app.get("/api/v1/traces/{trace_id}")
def trace(trace_id: str):
    return app.state.container.audit_store.by_trace(trace_id)
