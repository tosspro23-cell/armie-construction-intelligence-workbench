from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolCallEvent:
    """One model-requested tool call, complete and ready to execute.

    SPEC-M16: only emitted once a tool call's argument JSON is fully
    received -- a partial function-call fragment cannot be parsed or acted
    on, so no event for an in-progress tool call exists.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class AnswerChunkEvent:
    """One streamed fragment of the model's final natural-language answer.

    SPEC-M16 Invariants: only ever emitted for content the model is
    generating as its *final* answer (no more tool calls pending this
    turn) -- never for an intermediate "thinking" step, and never before
    every tool call this turn has already resolved.
    """

    text: str


@dataclass
class TurnCompleteEvent:
    """Marks the end of one model turn.

    ``tool_calls`` is non-empty when the model wants to call tools before
    answering (the caller executes them, appends their results, and starts
    another turn); empty when the model has finished streaming its final
    answer via ``AnswerChunkEvent``s. ``raw_assistant_message`` is the
    provider-native message dict to append to the running conversation
    history for the next turn's request.
    """

    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    raw_assistant_message: dict[str, Any] = field(default_factory=dict)
    # Independent-review finding, 2026-09-17: nothing previously checked
    # *why* the stream ended -- a response cut short by the model's own
    # max-token limit ("length") or blocked mid-answer by content
    # filtering ("content_filter") was treated identically to a normal,
    # complete "stop," so a truncated narrative could finalize as a
    # confident, complete-looking answer. `None` means the underlying
    # provider call didn't expose one (a fake/scripted provider in tests).
    finish_reason: str | None = None


class ModelProvider(Protocol):
    """Provider boundary used only for non-deterministic interpretation."""

    name: str
    model: str

    async def structured(
        self,
        *,
        prompt: str,
        response_model: type[T],
        purpose: str,
    ) -> T: ...

    async def vision_structured(
        self,
        *,
        prompt: str,
        image_base64: str,
        response_model: type[T],
        purpose: str,
    ) -> T: ...

    def stream_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        purpose: str,
    ) -> AsyncIterator[ToolCallEvent | AnswerChunkEvent | TurnCompleteEvent]:
        """SPEC-M16: one turn of the V2 tool-calling loop, streamed.

        A single underlying model call, streamed: if the model decides to
        call tools, their (complete, parsed) arguments arrive as
        ``ToolCallEvent``s once fully received, followed by a
        ``TurnCompleteEvent`` naming them all -- the caller executes them
        and starts a new turn. If the model decides to answer directly
        instead, its answer streams live as ``AnswerChunkEvent``s, followed
        by a ``TurnCompleteEvent`` with no tool calls. Never both in one
        turn: a real model turn either calls tools or answers, not partly
        each.
        """
        ...


class EmbeddingProvider(Protocol):
    """SPEC-M7: a separate, narrower Protocol from ModelProvider, not an
    extension of it. Embedding is Azure-only in this milestone (no Ollama/
    OpenAI implementation) and used only for the opt-in Azure AI Search
    retrieval path, never for interpretation -- folding it into
    ModelProvider would force every text/vision provider to answer for a
    capability most of them don't have.
    """

    name: str
    model: str

    async def embed(self, text: str) -> list[float]: ...

