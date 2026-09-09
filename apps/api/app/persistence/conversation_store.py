from __future__ import annotations

from typing import Protocol


class ConversationStore(Protocol):
    """Per-thread conversation context (SPEC-M4 §A).

    Replaces the plain ``dict`` that used to live at ``app.state.conversations``
    (created empty in ``main.py``'s ``lifespan`` on every process start, so a
    Container App revision replacement silently lost every in-flight thread's
    context -- D-012 Finding 9). ``InMemoryConversationStore`` below preserves
    that exact dict behaviour for local development and tests;
    ``PostgresConversationStore`` (persistence/postgres_store.py) is the
    production-durable implementation. Both satisfy this same two-method
    interface so call sites in main.py never need to know which one is live.
    """

    def get(self, thread_id: str) -> dict | None: ...

    def set(self, thread_id: str, context: dict) -> None: ...


class InMemoryConversationStore:
    """The pre-SPEC-M4 behaviour, given an explicit name and interface."""

    def __init__(self) -> None:
        self._contexts: dict[str, dict] = {}

    def get(self, thread_id: str) -> dict | None:
        return self._contexts.get(thread_id)

    def set(self, thread_id: str, context: dict) -> None:
        self._contexts[thread_id] = context
