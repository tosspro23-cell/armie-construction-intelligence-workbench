from __future__ import annotations

from typing import Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def _build_managed_identity_client(endpoint: str, api_version: str):
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
    )


class AzureOpenAIProvider:
    """Azure OpenAI provider, authenticated via Managed Identity only.

    Mirrors ``OpenAIProvider``'s request shape (the Responses API with a
    strict JSON schema) so the typed-plan contract this system depends on
    (D-001/D-002) is unaffected by which OpenAI-compatible endpoint answers
    the call. A ``client_factory`` seam is accepted so tests can substitute a
    fake client with no live Azure call and no ``azure-identity`` import,
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
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_version = api_version
        self.model = deployment
        self._client_factory = client_factory

    def _client(self):
        if not self.endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is required for Azure OpenAI provider calls.")
        if self._client_factory is not None:
            return self._client_factory()
        return _build_managed_identity_client(self.endpoint, self.api_version)

    async def structured(self, *, prompt: str, response_model: type[T], purpose: str) -> T:
        client = self._client()
        response = await client.responses.create(
            model=self.model,
            input=prompt,
            text={"format": {"type": "json_schema", "name": response_model.__name__,
                             "schema": response_model.model_json_schema(), "strict": True}},
        )
        return response_model.model_validate_json(response.output_text)

    async def vision_structured(
        self, *, prompt: str, image_base64: str, response_model: type[T], purpose: str
    ) -> T:
        client = self._client()
        response = await client.responses.create(
            model=self.model,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{image_base64}"},
                ],
            }],
            text={"format": {"type": "json_schema", "name": response_model.__name__,
                             "schema": response_model.model_json_schema(), "strict": True}},
        )
        return response_model.model_validate_json(response.output_text)
