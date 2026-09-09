from __future__ import annotations

from typing import Callable, TypeVar

from pydantic import BaseModel

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
