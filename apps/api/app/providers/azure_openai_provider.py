from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, TypeVar

from pydantic import BaseModel

from app.providers.base import AnswerChunkEvent, ToolCallEvent, TurnCompleteEvent

T = TypeVar("T", bound=BaseModel)

_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def _build_managed_identity_client(endpoint: str, api_version: str, timeout_seconds: float):
    """Construct the real Azure OpenAI client, Managed Identity only (OD-23).

    No API-key code path exists here: the only credential mechanism is an
    Azure AD bearer token acquired through ``DefaultAzureCredential``, so no
    Azure secret can ever be embedded in a container image or IaC template
    for this provider (SPEC-M3 §7).
    """
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from openai import AsyncAzureOpenAI

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SERVICES_SCOPE)
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_version=api_version,
        azure_ad_token_provider=token_provider,
        timeout=timeout_seconds,
    )


class AzureOpenAIProvider:
    """Azure OpenAI provider, authenticated via Managed Identity only.

    Mirrors ``OpenAIProvider``'s request shape (Chat Completions with a
    JSON-schema `response_format`, non-strict) so the typed-plan contract
    this system depends on (D-001/D-002) is unaffected by which
    OpenAI-compatible endpoint answers the call. Two things were found live
    during SPEC-M3's first real deployment, neither exercisable through
    `FakeModelProvider`, which bypasses real schema/transport behaviour
    entirely:

    1. Both providers originally used the newer Responses API; switched to
       Chat Completions after that route returned 404 on this Azure OpenAI
       resource (verified directly with a raw REST call against both
       `/openai/deployments/{deployment}/responses` and `/openai/responses`,
       independent of API version -- `/chat/completions` returned 401
       PermissionDenied for an unauthorized caller in the same probe,
       proving that route exists and this was never an auth problem).
    2. `strict: true` in `response_format` was tried first and rejected
       (400: "'additionalProperties' is required... to be false") because
       `QueryPlan.filters` is a free-form `dict` field -- OpenAI's Structured
       Outputs strict mode does not support arbitrary-key dict/mapping
       types at all, and `QueryPlan` is a protected, must-remain-stable
       contract (AGENT_HANDOFF.md), not something this milestone may
       restructure to fit strict mode's constraints. Non-strict JSON-schema
       guidance plus this system's existing `model_validate_json` (and its
       semantic-repair retry path for a mismatch) was already the exact
       reliability model the local Ollama provider uses, so this makes
       OpenAI/Azure consistent with it rather than introducing a new one.

    A ``client_factory`` seam is accepted so tests can substitute a fake
    client with no live Azure call and no ``azure-identity`` import,
    matching this repository's existing discipline of injecting factories
    rather than instances (D-007).
    """

    name = "azure"

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_version: str,
        deployment: str,
        timeout_seconds: float = 90.0,
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_version = api_version
        self.model = deployment
        # Independent review finding: never threaded through -- this
        # provider silently used openai's SDK default (600s), far past this
        # system's own request_timeout_seconds (180s, config.py).
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def _client(self):
        if not self.endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is required for Azure OpenAI provider calls.")
        if self._client_factory is not None:
            return self._client_factory()
        return _build_managed_identity_client(self.endpoint, self.api_version, self.timeout_seconds)

    async def structured(self, *, prompt: str, response_model: type[T], purpose: str) -> T:
        client = self._client()
        response = await client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema()}},
        )
        return response_model.model_validate_json(response.choices[0].message.content)

    async def vision_structured(
        self, *, prompt: str, image_base64: str, response_model: type[T], purpose: str
    ) -> T:
        client = self._client()
        response = await client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                ],
            }],
            response_format={"type": "json_schema", "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema()}},
        )
        return response_model.model_validate_json(response.choices[0].message.content)

    async def stream_turn(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], purpose: str,
    ) -> AsyncIterator[AnswerChunkEvent | ToolCallEvent | TurnCompleteEvent]:
        """SPEC-M16: one streamed V2 turn.

        A single `stream=True` call with `tools=` passed. Tool-call
        argument fragments arrive incrementally, indexed by their position
        in the model's response (the SDK's own accumulation convention --
        `delta.tool_calls[i].function.arguments` is a partial JSON string
        fragment, not a complete value, until the stream ends), so they are
        buffered here and only ever emitted as a complete, parsed
        `ToolCallEvent` once the stream itself ends with
        `finish_reason == "tool_calls"`. Plain answer content, in
        contrast, is never buffered -- each `delta.content` fragment is a
        real, already-final piece of the model's answer and is forwarded
        as an `AnswerChunkEvent` immediately, which is what makes this
        genuinely streamed rather than a single response chunked
        afterwards.
        """
        client = self._client()
        stream = await client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, stream=True,
        )
        # Keyed by the SDK's own per-call tool_call index -- the model can
        # request several tool calls in one turn, and their argument
        # fragments interleave across chunks by this index, not by order
        # of arrival.
        pending_calls: dict[int, dict[str, Any]] = {}
        content_parts: list[str] = []
        async for chunk in stream:
            # Confirmed live against the real Azure OpenAI deployment,
            # 2026-09-16: Azure's own streaming endpoint sends at least one
            # leading chunk (content-filter/prompt-annotation metadata)
            # with an empty `choices` array before any real delta arrives
            # -- indexing [0] unconditionally raised IndexError on every
            # single real streamed turn. Never observed against the plain
            # OpenAI API's own streaming shape, only Azure's.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                yield AnswerChunkEvent(text=delta.content)
            for tool_call_delta in delta.tool_calls or []:
                slot = pending_calls.setdefault(tool_call_delta.index, {"id": None, "name": "", "arguments": ""})
                if tool_call_delta.id:
                    slot["id"] = tool_call_delta.id
                if tool_call_delta.function and tool_call_delta.function.name:
                    slot["name"] += tool_call_delta.function.name
                if tool_call_delta.function and tool_call_delta.function.arguments:
                    slot["arguments"] += tool_call_delta.function.arguments
        tool_calls = [
            ToolCallEvent(call_id=slot["id"] or f"call_{index}", tool_name=slot["name"], arguments=json.loads(slot["arguments"] or "{}"))
            for index, slot in sorted(pending_calls.items())
        ]
        raw_message: dict[str, Any] = {"role": "assistant"}
        if tool_calls:
            raw_message["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.tool_name, "arguments": json.dumps(call.arguments)}}
                for call in tool_calls
            ]
        else:
            raw_message["content"] = "".join(content_parts)
        yield TurnCompleteEvent(tool_calls=tool_calls, raw_assistant_message=raw_message)


class AzureOpenAIEmbeddingProvider:
    """Azure OpenAI embeddings, Managed Identity only (OD-23 extended, SPEC-M7).

    A separate class from AzureOpenAIProvider, not a shared one: an
    embedding deployment is a genuinely different model shape (no chat
    completion, no vision), and reusing AzureOpenAIProvider's `.model` to
    also mean "the embedding deployment" would make `actual_model` audit
    fields ambiguous about which deployment actually served a given call.
    Reuses the same `_build_managed_identity_client` helper -- same
    resource, same credential, only the deployment name differs.
    """

    name = "azure"

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_version: str,
        deployment: str,
        timeout_seconds: float = 90.0,
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_version = api_version
        self.model = deployment
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def _client(self):
        if not self.endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is required for Azure OpenAI embedding calls.")
        if self._client_factory is not None:
            return self._client_factory()
        return _build_managed_identity_client(self.endpoint, self.api_version, self.timeout_seconds)

    async def embed(self, text: str) -> list[float]:
        client = self._client()
        response = await client.embeddings.create(model=self.model, input=text)
        return response.data[0].embedding
