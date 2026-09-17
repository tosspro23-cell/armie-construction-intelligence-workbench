"""Deterministic, no-network double for ``app.providers.base.ModelProvider``.

This is the *only* mechanism used in this test suite to drive the
probabilistic (LLM) path (SPEC-M1 §4.2/10, §8, D-007). Tests inject an
instance of this class through ``ServiceContainer``'s provider factories;
production ``app.providers.factory`` globals are never monkeypatched, so the
real injection seam stays under test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TypeVar

from app.providers.base import AnswerChunkEvent, ToolCallEvent, TurnCompleteEvent
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class RecordedCall:
    """One call made against a :class:`FakeModelProvider`."""

    kind: str  # "structured" | "vision_structured" | "stream_turn"
    purpose: str
    prompt: str
    response_model: type[BaseModel] | None = None
    image_base64: str | None = None


@dataclass
class ScriptedToolCalls:
    """SPEC-M16: script a ``stream_turn`` response requesting one or more
    tool calls, e.g. ``ScriptedToolCalls([("count_elements", {"entity_type": "IfcDoor"})])``.
    """

    calls: list[tuple[str, dict[str, Any]]]


@dataclass
class ScriptedAnswer:
    """SPEC-M16: script a ``stream_turn`` response streaming a final answer
    as the given chunks, e.g. ``ScriptedAnswer(["The project ", "contains 4 doors."])``.

    ``finish_reason`` defaults to ``None`` (a normal stop); pass
    ``"length"`` or ``"content_filter"`` to script the truncated/blocked-
    mid-answer case ``invoke_v2`` treats as a hard failure (see
    `TurnCompleteEvent.finish_reason`'s own docstring).
    """

    chunks: list[str] = field(default_factory=list)
    finish_reason: str | None = None


async def sleep_past_deadline(seconds: float, *, then: BaseException | BaseModel) -> Any:
    """Scriptable response: sleep, then raise or return ``then``.

    Pass as a zero-arg callable via ``functools.partial`` (or a lambda) when
    scripting a response so ``FakeModelProvider`` can simulate a provider
    call that exceeds a configured timeout without any real network I/O.
    """
    await asyncio.sleep(seconds)
    if isinstance(then, BaseException):
        raise then
    return then


class FakeModelProvider:
    """Scripted, in-memory ``ModelProvider`` implementation.

    Responses are scripted per ``purpose`` as an ordered queue. Each queued
    item may be:

    - a ``BaseModel`` instance: returned as-is (including a schema-valid but
      semantically-wrong payload — the caller controls its field values);
    - a ``BaseException`` instance: raised (e.g. ``StructuredOutputError``,
      a transport error, or ``asyncio.TimeoutError``);
    - a zero-arg async callable: awaited, and its result/exception is used
      (e.g. :func:`sleep_past_deadline`, to simulate exceeding a deadline).

    No network I/O occurs; this class has no Ollama/OpenAI dependency.
    """

    name: str
    model: str

    def __init__(self, *, name: str = "fake", model: str = "fake-model") -> None:
        self.name = name
        self.model = model
        self._queues: dict[str, list[Any]] = {}
        self.calls: list[RecordedCall] = []

    def script(self, purpose: str, *responses: Any) -> "FakeModelProvider":
        """Queue one or more responses/exceptions/callables for ``purpose``."""
        self._queues.setdefault(purpose, []).extend(responses)
        return self

    async def _resolve(self, purpose: str) -> Any:
        queue = self._queues.get(purpose)
        if not queue:
            raise AssertionError(
                f"FakeModelProvider({self.name}/{self.model}) has no scripted "
                f"response left for purpose={purpose!r}"
            )
        item = queue.pop(0)
        if asyncio.iscoroutinefunction(item) or (callable(item) and not isinstance(item, BaseModel)):
            item = item()
        if asyncio.iscoroutine(item):
            item = await item
        if isinstance(item, BaseException):
            raise item
        return item

    async def structured(self, *, prompt: str, response_model: type[T], purpose: str) -> T:
        self.calls.append(RecordedCall(kind="structured", purpose=purpose, prompt=prompt, response_model=response_model))
        return await self._resolve(purpose)

    async def vision_structured(
        self, *, prompt: str, image_base64: str, response_model: type[T], purpose: str
    ) -> T:
        self.calls.append(RecordedCall(
            kind="vision_structured", purpose=purpose, prompt=prompt,
            response_model=response_model, image_base64=image_base64,
        ))
        return await self._resolve(purpose)

    async def stream_turn(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], purpose: str,
    ) -> AsyncIterator[AnswerChunkEvent | ToolCallEvent | TurnCompleteEvent]:
        """SPEC-M16: script one turn with :class:`ScriptedToolCalls` or
        :class:`ScriptedAnswer`, queued via ``script(purpose, ...)`` exactly
        like every other purpose this fake already supports.
        """
        self.calls.append(RecordedCall(kind="stream_turn", purpose=purpose, prompt=str(messages)))
        item = await self._resolve(purpose)
        if isinstance(item, ScriptedToolCalls):
            events = [ToolCallEvent(call_id=f"call_{index}", tool_name=name, arguments=arguments) for index, (name, arguments) in enumerate(item.calls)]
            yield TurnCompleteEvent(tool_calls=events, raw_assistant_message={
                "role": "assistant",
                "tool_calls": [{"id": event.call_id, "type": "function", "function": {"name": event.tool_name, "arguments": "{}"}} for event in events],
            })
            return
        if isinstance(item, ScriptedAnswer):
            for chunk in item.chunks:
                yield AnswerChunkEvent(text=chunk)
            yield TurnCompleteEvent(tool_calls=[], raw_assistant_message={"role": "assistant", "content": "".join(item.chunks)}, finish_reason=item.finish_reason)
            return
        raise AssertionError(f"FakeModelProvider.stream_turn expected a ScriptedToolCalls or ScriptedAnswer for purpose={purpose!r}, got {item!r}")
