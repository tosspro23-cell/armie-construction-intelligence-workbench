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
from app.schemas.models import AgentResponse, AuditEvent, ChatRequest, ClarificationResumeRequest, Disposition, VerificationStatus
from app.services import ServiceContainer


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.container = ServiceContainer(settings)
    app.state.agent = AgentService(app.state.container)
    app.state.conversations = {}
    app.state.requests = {}
    yield


app = FastAPI(
    title="ARMIE Construction Intelligence Workbench",
    version="0.1.0",
    lifespan=lifespan,
)
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


def _terminal_response(request: ChatRequest, request_id: str, disposition: Disposition, message: str) -> AgentResponse:
    thread_id = request.thread_id or request_id
    return AgentResponse(
        thread_id=thread_id, trace_id=request_id, disposition=disposition, answer_markdown=message,
        verification=VerificationStatus(status="not_applicable", reason=message),
        execution_metadata={"request_id": request_id, "terminal": disposition.value},
    )


@app.post("/api/v1/chat")
async def chat(request: ChatRequest):
    agent: AgentService = app.state.agent
    contexts: dict = app.state.conversations
    request_id = request.request_id or str(uuid4())
    record = {"status": "running", "stage": "queued", "task": asyncio.current_task(), "trace_id": request_id}
    app.state.requests[request_id] = record
    context = contexts.get(request.thread_id or "", {})
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
        app.state.container.audit_store.append(AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="cancelled", summary="Request was cancelled before a terminal response.", payload={"request_id": request_id}))
        return _terminal_response(request, request_id, Disposition.CANCELLED, "Request cancelled. No result was committed to the conversation context.")
    except asyncio.TimeoutError:
        record.update(status="timeout", stage="timeout")
        app.state.container.audit_store.append(AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="timeout", summary="Request exceeded the bounded request deadline.", payload={"request_id": request_id, "timeout_seconds": app.state.container.settings.request_timeout_seconds}))
        return _terminal_response(request, request_id, Disposition.TIMEOUT, "The request exceeded its time limit. Partial audit events were preserved; please retry with a narrower question.")
    except Exception as error:
        record.update(status="error", stage="error", error=str(error))
        return _terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: {error}")
    record.update(status="completed", stage="completed", trace_id=response.trace_id)
    response = response.model_copy(update={
        "execution_metadata": {
            **response.execution_metadata,
            "request_id": request_id,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    })
    if response.disposition.value in {"answered", "clarification_required"}:
        context_update = response.context_update
        if context_update:
            contexts[response.thread_id] = context_update
        else:
            trace = app.state.container.audit_store.by_trace(response.trace_id)
            latest_plan = next((event.payload.get("plan") for event in reversed(trace) if event.event_type == "route_selected"), None)
            contexts[response.thread_id] = {
                "previous_query_plan": latest_plan,
                "active_entity_type": latest_plan.get("entity_type") if latest_plan else None,
                "active_filters": latest_plan.get("filters", {}) if latest_plan else {},
                "active_group_by": latest_plan.get("group_by") if latest_plan else None,
                "evidence_refs": [citation.evidence_id for citation in response.citations],
            }
    return response


@app.post("/api/v1/chat/{thread_id}/resume")
async def resume(thread_id: str, request: ClarificationResumeRequest):
    contexts: dict = app.state.conversations
    if thread_id not in contexts:
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
