from __future__ import annotations

import threading
from typing import Protocol


class ConversationStore(Protocol):
    """Per-thread conversation context (SPEC-M4 §A).

    Replaces the plain ``dict`` that used to live at ``app.state.conversations``
    (created empty in ``main.py``'s ``lifespan`` on every process start, so a
    Container App revision replacement silently lost every in-flight thread's
    context -- D-012 Finding 9). ``InMemoryConversationStore`` below preserves
    that exact dict behaviour for local development and tests;
    ``PostgresConversationStore`` (persistence/postgres_store.py) is the
    production-durable implementation. Both satisfy this same interface so
    call sites in main.py never need to know which one is live.
    """

    def get(self, thread_id: str) -> dict | None: ...

    def set(self, thread_id: str, context: dict) -> None: ...

    def bind_project(self, thread_id: str, project_id: str) -> str:
        """SPEC-M9 SS D: atomically claim `project_id` for `thread_id` if it
        has never been bound, or return the *already-bound* project_id
        unchanged otherwise -- a second, differently-projected request must
        never silently overwrite the first. Kept as a separate operation
        from get/set (not folded into `context`) so a thread that gets
        bound but never completes a real turn does not become
        indistinguishable from a thread that actually has conversation
        context -- main.py's resume() relies on get() returning None for
        the latter case.
        """
        ...


class InMemoryConversationStore:
    """The pre-SPEC-M4 behaviour, given an explicit name and interface."""

    def __init__(self) -> None:
        self._contexts: dict[str, dict] = {}
        self._project_bindings: dict[str, str] = {}
        # Store calls arrive via asyncio.to_thread -- real OS threads, not
        # just concurrent coroutines on one event loop -- so a plain dict
        # `setdefault` on _project_bindings is not race-safe without this.
        self._project_binding_lock = threading.Lock()

    def get(self, thread_id: str) -> dict | None:
        return self._contexts.get(thread_id)

    def set(self, thread_id: str, context: dict) -> None:
        self._contexts[thread_id] = context

    def bind_project(self, thread_id: str, project_id: str) -> str:
        with self._project_binding_lock:
            return self._project_bindings.setdefault(thread_id, project_id)
