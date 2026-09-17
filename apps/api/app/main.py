from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from opentelemetry import trace as otel_trace

from app.agent.graph import AgentService
from app.config import get_settings
from app.finding_workflow import (
    IllegalFindingTransition,
    resolve_reverify_outcome,
    validate_transition,
)
from app.persistence.conversation_store import ConversationStore
from app.rate_limit import InMemorySlidingWindowRateLimiter
from app.schemas.models import (
    AgentResponse,
    AuditEvent,
    ChatRequest,
    ClarificationResumeRequest,
    Disposition,
    FindingStatus,
    FindingTransitionRequest,
    VerificationStatus,
)
from app.security import (
    check_request_ownership,
    generate_session_id,
    require_api_key,
    require_rate_limit,
)
from app.services import ProjectNotFoundError, ServiceContainer
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
    # D-019: constructed regardless of whether rate limiting is actually
    # enabled -- require_rate_limit (app/security.py) returns before ever
    # consulting this when rate_limit_requests_per_minute is unset, so an
    # unconfigured deployment pays no cost for its existence.
    app.state.rate_limiter = InMemorySlidingWindowRateLimiter(
        limit=settings.rate_limit_requests_per_minute or 1, window_seconds=60.0,
    )
    # D-023 addendum: a second, caller-identity-independent limiter -- see
    # config.py's rate_limit_global_requests_per_minute docstring for why
    # the per-caller one above can never be the actual cost-protection
    # boundary on its own.
    app.state.global_rate_limiter = InMemorySlidingWindowRateLimiter(
        limit=settings.rate_limit_global_requests_per_minute or 1, window_seconds=60.0,
    )
    yield
    # Independent-review addition: PostgresConversationStore/PostgresAuditStore
    # (SPEC-M4) hold open psycopg connection pools; InMemoryConversationStore/
    # JsonlAuditStore hold none, so they don't define close() at all -- this
    # stays duck-typed rather than adding a no-op close() to the
    # ConversationStore/AuditStore Protocols, so those interfaces stay
    # exactly the two methods D-014 documents them as.
    for store in (app.state.container.conversation_store, app.state.container.audit_store):
        close = getattr(store, "close", None)
        if close is not None:
            close()


app = FastAPI(
    title="ARMIE Construction Intelligence Workbench",
    version="0.1.0",
    lifespan=lifespan,
    # App-wide, not per-route (SPEC-M5 §A): every route requires this
    # uniformly, including any added later, with no allowlist of "exempt"
    # routes to maintain. A no-op locally unless API_SHARED_SECRET is set
    # (app/security.py).
    dependencies=[Depends(require_api_key)],
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
# D-056: owner-reported, 2026-09-16 -- DigitalHub's first (uncached) 3D
# viewer load felt close to two minutes. Real, measured causes, not
# guessed: D-054 nearly tripled DigitalHub's rendered element count
# (303 -> 748) and its own `GET /api/v1/project/viewer-mesh` payload grew
# to a real, measured 15.2 MB of uncompressed JSON -- with nothing
# compressing it in transit. gzip on this exact payload (measured, same
# 748-element DigitalHub file) compresses it to 3.5 MB (4.3x) for ~0.6s of
# server-side CPU, a real net win on any connection slower than very fast
# broadband. `minimum_size` keeps small JSON responses (most of this
# app's other routes) uncompressed, since gzip's own per-request overhead
# isn't worth paying for a response that's already a few hundred bytes.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.post("/api/v1/session")
def create_session() -> dict:
    """Issues a per-tab caller-correlation token (D-016). Not a new
    authentication boundary -- reaching this route already requires the
    shared secret (require_api_key, app-wide) -- only a way for
    app.state.requests to tell two concurrent holders of that secret
    apart. The frontend calls this once per gate-unlock
    (apps/web/src/apiClient.tsx ensureSessionId) and threads the result
    back as ``X-Session-Id`` on every later request.
    """
    return {"session_id": generate_session_id()}


@app.get("/api/v1/health")
def health() -> dict:
    container: ServiceContainer = app.state.container
    return {"status": "ok", "project": container.project_metadata()}


async def _resolve_project(project_id: str) -> tuple:
    """Shared by every /api/v1/project/* endpoint below (SPEC-M9): resolves
    `project_id` (default "demo") to its ProjectResources, or a 404 for an
    unknown project -- never a silent fallback to "demo".
    """
    container: ServiceContainer = app.state.container
    try:
        return container, await container.get_project(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown project '{project_id}'.") from None


@app.get("/api/v1/projects")
def list_projects() -> list[dict]:
    """SPEC-M9 §E: the frontend's project selector data source. Empty when
    ADLS multi-project mode is off, so the control can hide itself entirely
    rather than presenting a meaningless single-item ("demo"-only) choice.
    """
    container: ServiceContainer = app.state.container
    if not container.settings.adls_account_url:
        return []
    return [
        {"project_id": project_id, "display_name": manifest.display_name}
        for project_id, manifest in container.project_registry.items()
    ]


@app.get("/api/v1/project/metadata")
async def project_metadata(project_id: str = "demo") -> dict:
    container, resources = await _resolve_project(project_id)
    metadata = {
        "ifc_available": resources.ifc_repository.available,
        "pdf_available": resources.document_analyzer.available,
        "ifc_file": resources.manifest.ifc_file,
        "pdf_file": resources.document_analyzer.pdf_path.name,
        "pdf_files": list(resources.document_analyzers.keys()),
        "project_id": resources.manifest.project_id,
        "capabilities": container.project_metadata()["capabilities"],
    }
    # asyncio.to_thread, not a direct call: independent-review finding
    # (P2 #10), confirmed live 2026-09-13 -- SPEC-M9 made this route
    # `async def` (needed for the `await _resolve_project` above, since a
    # cold ADLS-mode project load is a real, legitimately-async download),
    # but a plain `def` FastAPI route runs in Starlette's own external
    # threadpool automatically, so the CPU/file work below used to get
    # that offload for free before this milestone -- an `async def` route
    # runs directly on the event loop instead, and neither call here has
    # any `await` of its own, so both would otherwise block every other
    # concurrent request, health check, and cancellation for as long as
    # they take. `ifc_repository.metadata()` is a plain method (not
    # cached -- only `.model`, which it reads from, is a
    # `@cached_property`), and `document_analyzer.inspect()` re-opens/
    # re-reads the PDF's first page on every single call -- both
    # genuinely block on every request, not just a cold project's first.
    if metadata["ifc_available"]:
        metadata["ifc"] = await asyncio.to_thread(resources.ifc_repository.metadata)
    if metadata["pdf_available"]:
        # One inspect() call per configured document, not one-plus-a-
        # redundant-extra: `document_analyzer` (singular) is always the
        # first entry of `document_analyzers`, so `metadata["pdf"]` is
        # read from this same gather's first result rather than issuing a
        # second, duplicate inspect() call for that document. Per-document
        # page counts/sizes for every configured document (not just the
        # default one) feed the drawing viewer's document switcher and
        # page stepper -- found missing live, 2026-09-13: a multi-document
        # project could previously only ever show page 1 of the first
        # document -- and let a citation's evidence marker use that
        # document's real page size instead of an assumed one.
        inspected_documents = await asyncio.gather(
            *(asyncio.to_thread(analyzer.inspect) for analyzer in resources.document_analyzers.values())
        )
        metadata["pdf"] = inspected_documents[0]
        metadata["pdf_documents"] = [
            {"filename": name, "page_count": inspected["page_count"], "page_sizes": inspected["page_sizes"]}
            for name, inspected in zip(resources.document_analyzers.keys(), inspected_documents)
            if inspected["available"]
        ]
    return metadata


@app.get("/api/v1/project/ifc")
async def project_ifc(project_id: str = "demo"):
    """Serve the configured local IFC only to the local viewer workflow."""
    _, resources = await _resolve_project(project_id)
    path = resources.ifc_repository.path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Configured IFC source file was not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@app.get("/api/v1/project/pdf")
async def project_pdf(project_id: str = "demo"):
    # The primary (first-configured) document only -- SPEC-M6 deliberately
    # does not make this viewer endpoint multi-document-aware; see
    # ServiceContainer.document_analyzer.
    _, resources = await _resolve_project(project_id)
    path = resources.document_analyzer.pdf_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Configured PDF source file was not found.")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/api/v1/project/pdf/pages/{page_number}.png")
async def project_pdf_page(page_number: int, project_id: str = "demo", document: str | None = None):
    _, resources = await _resolve_project(project_id)
    # `document` lets the drawing viewer page through any configured
    # document, not only the default one `resources.document_analyzer`
    # picks -- SPEC-M6's own answer pipeline (_execute_pdf_multi_document)
    # has been multi-document-aware since that milestone, but this viewer
    # endpoint wasn't (a deliberate MVP scope note at the time), and the
    # gap was never closed when the corpus grew past one document. Found
    # live, 2026-09-13. Optional and defaulted for backward compatibility:
    # every existing caller that never passed `document` keeps behaving
    # exactly as before.
    analyzer = resources.document_analyzers.get(document) if document else resources.document_analyzer
    if analyzer is None:
        raise HTTPException(status_code=404, detail=f"Document '{document}' is not part of the configured corpus.")
    if page_number < 1 or not analyzer.available:
        raise HTTPException(status_code=404, detail="Requested PDF page was not found.")
    # asyncio.to_thread: see project_metadata's own comment above for why
    # an async def route here needs this explicitly -- render_page is the
    # most expensive of these three (PyMuPDF rasterization + a PNG write
    # to disk, uncached, on every single call).
    rendered_path = await asyncio.to_thread(analyzer.render_page, page_number, scale=1.5)
    return FileResponse(rendered_path, media_type="image/png")


@app.get("/api/v1/evidence/{filename}")
def evidence_file(filename: str):
    # Basename containment prevents this local-review endpoint from exposing
    # arbitrary paths while allowing PDF crops and rendered pages in the UI.
    # `filename in {".", ".."}` is explicit, not redundant with the check
    # below: `Path("..").name` returns ".." unchanged (a pathlib quirk --
    # only a path *containing* "/" changes under `.name`), so a bare ".."
    # segment previously passed this check and reached FileResponse with
    # evidence_dir's own parent directory, an unhandled RuntimeError (500)
    # rather than the clean 400 this endpoint intends for any invalid
    # input. Found in self-review, 2026-09-13; see
    # tests/test_evidence_blob_persistence.py's regression test for the
    # real (percent-encoded, client-normalization-bypassing) reproduction.
    if filename in {".", ".."} or Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid evidence file name.")
    # SPEC-M8: blob storage first when configured -- it is the durable
    # source of truth once enabled (a Container App revision replacement
    # wipes local disk, but not blob storage); local is still checked as
    # a fallback, covering evidence written before this was enabled or a
    # crop whose upload transiently failed (DocumentAnalyzer.crop_evidence
    # never fails the request over that, so local remains the only copy
    # in that case).
    container: ServiceContainer = app.state.container
    blob_container_client = container.blob_container_client_factory(container.settings)
    if blob_container_client is not None:
        try:
            downloaded = blob_container_client.download_blob(filename).readall()
            return Response(content=downloaded, media_type="image/png")
        except Exception:
            pass
    path = app.state.container.settings.evidence_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Evidence file was not found.")
    return FileResponse(path)


@app.get("/api/v1/project/viewer-elements")
async def project_viewer_elements(project_id: str = "demo") -> dict:
    """Expose a lightweight projection of the real local IFC for the browser
    viewer. SPEC-M9: confirmed live by independent review that
    apps/web/src/IfcViewer.tsx calls this endpoint directly, independently
    of apps/web/src/main.tsx's own request logic -- a project selector
    added only to main.tsx would not have reached this call site.
    """
    _, resources = await _resolve_project(project_id)
    repository = resources.ifc_repository
    if not repository.available:
        raise HTTPException(status_code=404, detail="Configured IFC source file was not found.")
    # asyncio.to_thread: see project_metadata's own comment above. Only
    # the first access per loaded IfcRepository instance genuinely parses
    # geometry (`@cached_property`), but that first parse is real
    # ifcopenshell work worth deferring off the event loop regardless.
    elements = await asyncio.to_thread(lambda: repository.viewer_elements)
    return {
        "source_file": repository.path.name,
        "representation": "ifcopenshell_bounding_geometry",
        "elements": elements,
    }


@app.get("/api/v1/project/viewer-mesh")
async def project_viewer_mesh(project_id: str = "demo") -> dict:
    """SPEC-M12: the real, `ifcopenshell`-triangulated geometry
    `project_viewer_elements` above deliberately discards down to a
    bounding box -- an additive representation, not a replacement;
    `viewer-elements` is unchanged for any other consumer.
    """
    _, resources = await _resolve_project(project_id)
    repository = resources.ifc_repository
    if not repository.available:
        raise HTTPException(status_code=404, detail="Configured IFC source file was not found.")
    # asyncio.to_thread: see project_viewer_elements's own comment above --
    # only the first access per loaded IfcRepository instance genuinely
    # computes geometry (`@cached_property`), but for a real building that
    # first compute is seconds of real ifcopenshell work, well worth
    # deferring off the event loop.
    elements = await asyncio.to_thread(lambda: repository.mesh_elements)
    return {
        "source_file": repository.path.name,
        "representation": "ifcopenshell_triangulated_mesh",
        "elements": elements,
    }


def _terminal_response(request: ChatRequest, request_id: str, disposition: Disposition, message: str, **extra_metadata) -> AgentResponse:
    thread_id = request.thread_id or request_id
    return AgentResponse(
        thread_id=thread_id, trace_id=request_id, disposition=disposition, answer_markdown=message,
        verification=VerificationStatus(status="not_applicable", reason=message),
        execution_metadata={"request_id": request_id, "terminal": disposition.value, **extra_metadata},
    )


def _tag_span_with_trace_id(response: AgentResponse) -> AgentResponse:
    """D-028/D-031: tags the current request's own OpenTelemetry span with
    the trace_id AuditStore/citations/`/api/v1/traces/{trace_id}`/
    `execution_metadata.cloud_trace_url` all actually key on --
    `response.trace_id`, not the caller-supplied `request_id` chat() also
    carries.

    Found live, 2026-09-14, from the owner's own Application Insights
    query returning zero rows for a real, successful answer's Cloud
    Provenance link: `_terminal_response` above happens to set
    `trace_id=request_id` for every early-exit path, but the successful
    path's `response` comes from `AgentService.invoke`, which always mints
    its own fresh `trace_id` internally (`str(uuid4())`, `graph.py`)
    completely independent of `request_id` -- confirmed by reading
    `AgentService.invoke`'s own `GraphState` construction, not assumed.
    Tagging with `request_id` before that call, as the original D-028 fix
    did, tagged the span with a UUID nothing else in the system ever
    looks up an answered response by. Called once, on the actual response
    about to be returned, covering every exit path uniformly instead of
    needing to know which of the two IDs a given branch happens to use.

    Safe to call unconditionally: see D-028's own note on
    `get_current_span()` being a harmless no-op when telemetry isn't
    configured.
    """
    otel_trace.get_current_span().set_attribute("app.trace_id", response.trace_id)
    return response


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


@app.post("/api/v1/chat", dependencies=[Depends(require_rate_limit)])
async def chat(request: ChatRequest, x_session_id: str | None = Header(default=None)):
    # SPEC-M16: engine="v2" is a completely separate handler/response shape
    # (a streamed SSE response, not a plain JSON AgentResponse) -- kept as
    # an early, explicit dispatch rather than threaded through the rest of
    # this function, so V1's own code path below is provably untouched
    # (byte-for-byte, per the spec's own Invariants) for every caller that
    # omits `engine` or passes "v1".
    if request.engine == "v2":
        return await _chat_v2(request, x_session_id)
    agent: AgentService = app.state.agent
    container: ServiceContainer = app.state.container
    conversations: ConversationStore = container.conversation_store
    request_id = request.request_id or str(uuid4())
    # SPEC-M9: resolved here, once, rather than left to AgentService.invoke's
    # own uuid4() fallback -- ConversationStore.bind_project needs a
    # concrete thread_id *before* invoke ever runs, so the project-mismatch
    # check below can reject a request before any tool/model call.
    thread_id = request.thread_id or str(uuid4())
    requested_project_id = request.project_id or "demo"
    # Independent-review finding, confirmed live (2026-09-13): request_id
    # is client-supplied (ChatRequest.request_id, defaulting to a fresh
    # uuid4 only when omitted) with no prior uniqueness check -- a second
    # caller supplying an in-flight request_id silently overwrote the
    # first caller's app.state.requests entry, including its session_id,
    # so the original caller's later GET/cancel against that id 404ed
    # (check_request_ownership comparing against the *new* record) even
    # though their own request was still genuinely running. No `await`
    # between this check and the dict write below, so this is race-safe
    # on this process's single event loop without needing a lock. No
    # idempotency/retry contract is defined for a reused request_id (the
    # omitted-request_id path already covers "start a fresh request"), so
    # this rejects outright rather than inventing one.
    if request_id in app.state.requests:
        raise HTTPException(status_code=409, detail=f"Request id '{request_id}' is already in use.")
    # session_id (D-016): whoever's X-Session-Id created this record is
    # its only owner for /api/v1/requests/* below; None (no header sent)
    # keeps the record unrestricted, matching pre-D-016 behaviour.
    record = {"status": "running", "stage": "queued", "task": asyncio.current_task(), "trace_id": request_id, "session_id": x_session_id}
    app.state.requests[request_id] = record
    try:
        bound_project_id = await asyncio.to_thread(conversations.bind_project, thread_id, requested_project_id)
    except Exception as error:
        record.update(status="error", stage="project_bind_error", error=str(error))
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: could not resolve project binding: {error}"))
    if bound_project_id != requested_project_id:
        record.update(status="error", stage="project_mismatch")
        raise HTTPException(
            status_code=409,
            detail=f"This thread is bound to project '{bound_project_id}', not '{requested_project_id}'. "
                   "Start a new conversation to switch projects.",
        )
    # started here, not just before agent.invoke below: this is the actual
    # start of this request's bounded deadline (request_timeout_seconds),
    # not only the model/tool-execution portion of it. Independent-review
    # finding, confirmed live (2026-09-13): get_project's own download-
    # and-verify path (ADLS mode) previously ran with no timeout of its
    # own at all -- a slow or hanging Data Lake read could block this
    # request indefinitely with no honest terminal disposition, and
    # whatever time it did take was never counted against the deadline
    # the client was told this request was bounded by.
    record["stage"] = "resolving_project"
    started = time.perf_counter()
    settings = app.state.container.settings
    try:
        project_resources = await asyncio.wait_for(container.get_project(bound_project_id), timeout=settings.request_timeout_seconds)
    except ProjectNotFoundError:
        record.update(status="error", stage="project_not_found")
        raise HTTPException(status_code=404, detail=f"Unknown project '{bound_project_id}'.") from None
    except asyncio.TimeoutError:
        record.update(status="timeout", stage="project_load_timeout")
        audit_error = await _safe_audit_append(app.state.container.audit_store, AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="timeout", summary="Loading the project's source files exceeded the bounded request deadline.", payload={"request_id": request_id, "project_id": bound_project_id, "timeout_seconds": settings.request_timeout_seconds}, project_id=bound_project_id))
        extra = {"audit_persist_error": audit_error} if audit_error else {}
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.TIMEOUT, "The request exceeded its time limit while loading the project's source files. Please retry shortly.", **extra))
    except Exception as error:
        # Independent-review finding, confirmed live (2026-09-13): this
        # previously had no except-Exception branch at all, so an ADLS
        # download failure (network error, a corrupted-content hash
        # mismatch, an unreachable Data Lake account) propagated as an
        # unhandled exception -- a bare 500 with no honest disposition,
        # and app.state.requests[request_id] was never updated to a
        # terminal status, so GET /api/v1/requests/{request_id} reported
        # "running" forever for a request that had already failed.
        record.update(status="error", stage="project_load_error", error=str(error))
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: could not load the project's source files: {error}"))
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
        context = await asyncio.to_thread(conversations.get, thread_id) or {}
    except Exception as error:
        record.update(status="error", stage="context_read_error", error=str(error))
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: could not read conversation context: {error}"))
    record["stage"] = "agent_execution"
    # The remaining budget, not a fresh full-length window: the deadline
    # this endpoint promises the client is for the whole request
    # (project resolution included, per the fix above), never restarted
    # partway through.
    remaining_budget = max(0.0, settings.request_timeout_seconds - (time.perf_counter() - started))
    try:
        response = await asyncio.wait_for(asyncio.to_thread(
            agent.invoke,
            project_resources=project_resources,
            thread_id=thread_id,
            question=request.question,
            viewer_context=request.viewer_context.model_dump() if request.viewer_context else None,
            conversation_context=context,
            source_preference=request.source_preference,
        ), timeout=remaining_budget)
    except asyncio.CancelledError:
        record.update(status="cancelled", stage="cancelled")
        audit_error = await _safe_audit_append(app.state.container.audit_store, AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="cancelled", summary="Request was cancelled before a terminal response.", payload={"request_id": request_id}, project_id=project_resources.manifest.project_id, source_set_id=project_resources.manifest.source_set_id))
        extra = {"audit_persist_error": audit_error} if audit_error else {}
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.CANCELLED, "Request cancelled. No result was committed to the conversation context.", **extra))
    except asyncio.TimeoutError:
        record.update(status="timeout", stage="timeout")
        audit_error = await _safe_audit_append(app.state.container.audit_store, AuditEvent(trace_id=request_id, thread_id=request.thread_id or request_id, step="request", event_type="timeout", summary="Request exceeded the bounded request deadline.", payload={"request_id": request_id, "timeout_seconds": settings.request_timeout_seconds}, project_id=project_resources.manifest.project_id, source_set_id=project_resources.manifest.source_set_id))
        extra = {"audit_persist_error": audit_error} if audit_error else {}
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.TIMEOUT, "The request exceeded its time limit. Partial audit events were preserved; please retry with a narrower question.", **extra))
    except Exception as error:
        record.update(status="error", stage="error", error=str(error))
        return _tag_span_with_trace_id(_terminal_response(request, request_id, Disposition.ERROR, f"The request failed safely: {error}"))

    response = response.model_copy(update={
        "execution_metadata": {
            **response.execution_metadata,
            "request_id": request_id,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            # SPEC-M9 SS F, D-023 addendum: independent-review finding,
            # confirmed live (2026-09-13) -- see Citation/AuditEvent's own
            # project_id/source_set_id fields for the full rationale.
            "project_id": project_resources.manifest.project_id,
            "source_set_id": project_resources.manifest.source_set_id,
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
    return _tag_span_with_trace_id(response)


async def _v2_sse_stream(agent: AgentService, conversations: ConversationStore, *, project_resources, thread_id: str, question: str, viewer_context: dict | None, recent_turns: list[dict[str, str]], context: dict, memory_turns: int, request_id: str, started: float, deadline: float, record: dict):
    """SPEC-M16 SS F: renders `AgentService.invoke_v2`'s event stream as
    Server-Sent Events. Each line is `data: <json>\\n\\n`, the format
    `EventSource`/a `ReadableStream` reader on the frontend can consume
    incrementally -- tool-use status and answer tokens are forwarded the
    moment they arrive, not buffered until the turn completes.

    `deadline`/`record` (independent-review finding, 2026-09-17): V2
    requests never shared V1's own request lifecycle at all -- not
    registered in `app.state.requests`, so `/api/v1/requests/{id}` and its
    `/cancel` counterpart 404 for any V2 request_id, and bounded only by
    `tool_calling_max_iterations` (iteration count), never by
    `request_timeout_seconds` (wall-clock time) the way V1's own
    `asyncio.wait_for(agent.invoke, ...)` is. `record` is this request's
    own entry in `app.state.requests` (created by `_chat_v2` below,
    mirroring `chat()`'s own registration) -- read here for cancellation,
    updated here with the real terminal status once the turn actually
    finishes, matching what `chat()` already does for V1.
    """
    import json as _json

    final_response = None
    try:
        async for event in agent.invoke_v2(
            project_resources=project_resources, thread_id=thread_id, question=question, viewer_context=viewer_context,
            recent_turns=recent_turns, deadline=deadline, cancel_check=lambda: record.get("status") == "cancel_requested",
        ):
            if event["type"] == "final":
                # Owner-reported, 2026-09-16: V2 responses never carried a
                # total latency (the Result step's "~X s" figure and the
                # per-step breakdown line both depend on
                # execution_metadata.latency_ms) -- `started`/`request_id`
                # were already threaded into this function for exactly this,
                # but nothing ever used them. Mirrors chat()'s own
                # post-`agent.invoke` enrichment above (D-023 addendum) so
                # both engines' responses carry the same fields.
                final_response = event["response"]
                execution_metadata = {
                    **final_response.execution_metadata,
                    "request_id": request_id,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "project_id": project_resources.manifest.project_id,
                    "source_set_id": project_resources.manifest.source_set_id,
                }
                # Independent-review finding, 2026-09-17: this used to run
                # *after* the "final" SSE line was already yielded (a
                # client already showing the answer, or one that
                # disconnects the instant it has what it asked for, could
                # both race past this) and any failure was a bare
                # `except: pass` -- an already-delivered turn could
                # silently fail to persist, and the *next* turn would
                # appear to have forgotten it with zero diagnostic trail.
                # Persisting before the response is finalized, and folding
                # a failure into execution_metadata, mirrors exactly what
                # chat()'s own `context_persist_error` (D-014) already does
                # for V1's conversation-context writes.
                context_persist_error: str | None = None
                if final_response.disposition.value == "answered":
                    updated_turns = (recent_turns + [{"question": question, "answer": final_response.answer_markdown}])[-memory_turns:]
                    try:
                        await asyncio.to_thread(conversations.set, thread_id, {**context, "v2_recent_turns": updated_turns})
                    except Exception as error:
                        context_persist_error = str(error)
                if context_persist_error:
                    execution_metadata["context_persist_error"] = context_persist_error
                final_response = final_response.model_copy(update={"execution_metadata": execution_metadata})
                record["status"] = "completed" if final_response.disposition.value not in {"timeout", "cancelled", "error"} else final_response.disposition.value
                payload = {"type": "final", "response": _json.loads(final_response.model_dump_json())}
            else:
                payload = event
            yield f"data: {_json.dumps(payload, default=str)}\n\n"
    except asyncio.CancelledError:
        record["status"] = "cancelled"
        raise
    except Exception as error:
        record.update(status="error", error=str(error))
        yield f"data: {_json.dumps({'type': 'error', 'message': f'The request failed safely: {error}'})}\n\n"
        return


async def _chat_v2(request: ChatRequest, x_session_id: str | None):
    """SPEC-M16: the V2 tool-calling agent's own request handler.

    Deliberately a separate function from `chat()` above, not a branch
    threaded through it -- V1's handler is complex enough that sharing
    control flow risks changing its behavior by accident; duplicating the
    handful of genuinely shared steps (thread/project resolution) here
    keeps that risk at zero, matching this spec's own Invariant that V1
    is byte-for-byte unaffected by this work.
    """
    agent: AgentService = app.state.agent
    container: ServiceContainer = app.state.container
    conversations: ConversationStore = container.conversation_store
    settings = container.settings
    # Independent-review finding, 2026-09-17: V2 requests were never
    # registered in `app.state.requests` at all -- confirmed live, both
    # `/api/v1/requests/{id}` and its `/cancel` counterpart 404 for any V2
    # request_id, and there was no overall wall-clock deadline covering
    # the model/tool-execution portion of the turn (only project loading
    # had one). Registered here the same way, and at the same point in
    # the request's own lifecycle, as `chat()` already does for V1 --
    # including the same request_id-reuse rejection (D-0xx, confirmed
    # live 2026-09-13 for V1's own path).
    request_id = request.request_id or str(uuid4())
    if request_id in app.state.requests:
        raise HTTPException(status_code=409, detail=f"Request id '{request_id}' is already in use.")
    record = {"status": "running", "stage": "queued", "task": asyncio.current_task(), "trace_id": request_id, "session_id": x_session_id}
    app.state.requests[request_id] = record
    started = time.perf_counter()
    deadline = started + settings.request_timeout_seconds
    thread_id = request.thread_id or str(uuid4())
    requested_project_id = request.project_id or "demo"
    try:
        bound_project_id = await asyncio.to_thread(conversations.bind_project, thread_id, requested_project_id)
    except Exception as error:
        record.update(status="error", stage="project_bind_error", error=str(error))
        raise HTTPException(status_code=503, detail=f"Could not resolve project binding: {error}") from error
    if bound_project_id != requested_project_id:
        record.update(status="error", stage="project_mismatch")
        raise HTTPException(status_code=409, detail=f"This thread is bound to project '{bound_project_id}', not '{requested_project_id}'. Start a new conversation to switch projects.")
    record["stage"] = "resolving_project"
    try:
        project_resources = await asyncio.wait_for(container.get_project(bound_project_id), timeout=settings.request_timeout_seconds)
    except ProjectNotFoundError:
        record.update(status="error", stage="project_not_found")
        raise HTTPException(status_code=404, detail=f"Unknown project '{bound_project_id}'.") from None
    except asyncio.TimeoutError:
        record.update(status="timeout", stage="project_load_timeout")
        raise HTTPException(status_code=504, detail="Loading the project's source files exceeded the bounded request deadline.") from None
    try:
        context = await asyncio.to_thread(conversations.get, thread_id) or {}
    except Exception as error:
        record.update(status="error", stage="context_read_error")
        raise HTTPException(status_code=503, detail=f"Could not read conversation context: {error}") from error
    recent_turns = context.get("v2_recent_turns", [])[-settings.conversation_memory_turns:]
    record["stage"] = "streaming"
    return StreamingResponse(
        _v2_sse_stream(
            agent, conversations, project_resources=project_resources, thread_id=thread_id, question=request.question,
            viewer_context=request.viewer_context.model_dump() if request.viewer_context else None,
            recent_turns=recent_turns, context=context, memory_turns=settings.conversation_memory_turns,
            request_id=request_id, started=started, deadline=deadline, record=record,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Thread-Id": thread_id},
    )


@app.post("/api/v1/chat/{thread_id}/resume", dependencies=[Depends(require_rate_limit)])
async def resume(thread_id: str, request: ClarificationResumeRequest, x_session_id: str | None = Header(default=None)):
    conversations: ConversationStore = app.state.container.conversation_store
    try:
        existing = await asyncio.to_thread(conversations.get, thread_id)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Could not read conversation state: {error}") from error
    if existing is None:
        raise HTTPException(status_code=404, detail="No resumable conversation was found for this thread.")
    # SPEC-M9: found live by independent review -- this previously
    # reconstructed ChatRequest with no project information at all, so a
    # resume on any project's thread silently answered against "demo".
    # bind_project on an already-bound thread just returns the existing
    # binding unchanged (never "demo" unless that is genuinely what was
    # bound) -- the source of truth, never request.project_id, which is
    # accepted only for an optional defense-in-depth consistency check.
    try:
        bound_project_id = await asyncio.to_thread(conversations.bind_project, thread_id, "demo")
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Could not resolve project binding: {error}") from error
    if request.project_id is not None and request.project_id != bound_project_id:
        raise HTTPException(
            status_code=409,
            detail=f"This thread is bound to project '{bound_project_id}', not '{request.project_id}'.",
        )
    return await chat(
        ChatRequest(thread_id=thread_id, question=request.answer, project_id=bound_project_id),
        x_session_id=x_session_id,
    )


@app.post("/api/v1/requests/{request_id}/cancel")
async def cancel_request(request_id: str, x_session_id: str | None = Header(default=None)):
    record = app.state.requests.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request was not found.")
    check_request_ownership(record, x_session_id)
    record["status"] = "cancel_requested"
    task = record.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    return {"request_id": request_id, "status": "cancel_requested"}


@app.get("/api/v1/requests/{request_id}")
def request_status(request_id: str, x_session_id: str | None = Header(default=None)):
    record = app.state.requests.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request was not found.")
    check_request_ownership(record, x_session_id)
    return {key: value for key, value in record.items() if key != "task"}


@app.get("/api/v1/traces/{trace_id}")
def trace(trace_id: str):
    return app.state.container.audit_store.by_trace(trace_id)


@app.get("/api/v1/findings")
def list_findings(project_id: str = "demo", status: FindingStatus | None = None) -> list[dict]:
    """SPEC-M11 §4C. `require_api_key` already covers this app-wide -- no
    new auth surface for this or any route below.
    """
    container: ServiceContainer = app.state.container
    return [item.model_dump() for item in container.finding_store.list_for_project(project_id, status)]


@app.get("/api/v1/findings/{finding_id}")
def get_finding(finding_id: str) -> dict:
    container: ServiceContainer = app.state.container
    finding = container.finding_store.get(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"Finding '{finding_id}' was not found.")
    return finding.model_dump()


@app.post("/api/v1/findings/{finding_id}/transition")
def transition_finding(finding_id: str, request: FindingTransitionRequest, x_session_id: str | None = Header(default=None)) -> dict:
    """SPEC-M11 §4C/§5: `validate_transition` is the single source of truth
    for legality -- an illegal action is a 409, never a silent no-op or an
    unhandled 500. `x_session_id` (D-016's per-tab correlation token, not a
    real login -- see the model's own field docstring) is recorded as the
    acting "human" for this transition.
    """
    container: ServiceContainer = app.state.container
    finding = container.finding_store.get(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"Finding '{finding_id}' was not found.")
    try:
        target_status = validate_transition(finding.status, request.action)
    except IllegalFindingTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    updated = container.finding_store.append_transition(
        finding_id, to_status=target_status, actor_session_id=x_session_id, note=request.note,
    )
    return updated.model_dump()


@app.post("/api/v1/findings/{finding_id}/reverify")
async def reverify_finding(finding_id: str, x_session_id: str | None = Header(default=None)) -> dict:
    """SPEC-M11 §4C: the actual closed-loop claim. Legal only from
    RESOLVED; re-reads the real IFC/PDF sources for this finding's own tag
    (`AgentService.reverify_reconciliation_tag`, zero model calls, the same
    comparison a full reconciliation run uses) rather than trusting the
    human's own "I fixed it" click -- transitions to VERIFIED_CLOSED only
    if that fresh read now agrees, otherwise bounces back to
    ACTION_REQUIRED with the newly-observed values.
    """
    container: ServiceContainer = app.state.container
    agent: AgentService = app.state.agent
    finding = container.finding_store.get(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"Finding '{finding_id}' was not found.")
    if finding.status != FindingStatus.RESOLVED:
        raise HTTPException(status_code=409, detail=f"Cannot re-verify a finding in status '{finding.status.value}'; only RESOLVED findings can be re-verified.")
    _, resources = await _resolve_project(finding.project_id)
    fresh = await asyncio.to_thread(agent.reverify_reconciliation_tag, resources, finding.tag)
    now_matches = fresh["status"] == "matched"
    target_status = resolve_reverify_outcome(finding.status, now_matches=now_matches)
    updated = container.finding_store.append_transition(
        finding_id, to_status=target_status, actor_session_id=x_session_id,
        note="Re-verified against the current sources." if now_matches else "Re-verify found the mismatch still present.",
        updates={
            "detail": fresh["detail"], "ifc_width_m": fresh["ifc_width_m"], "ifc_height_m": fresh["ifc_height_m"],
            "pdf_width_m": fresh["pdf_width_m"], "pdf_height_m": fresh["pdf_height_m"],
        },
    )
    return updated.model_dump()
