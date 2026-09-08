"""AzureOpenAIProvider tests (SPEC-M3 §8).

Zero live Azure calls, mirroring FakeModelProvider's no-network discipline
(D-007): every test injects a fake ``client_factory`` instead of importing
or contacting ``azure-identity``/Azure OpenAI. Covers the two acceptance
criteria named in SPEC-M3 §8: a successful structured/vision-structured call
against a mocked client, and a credential/token-acquisition failure
propagating cleanly rather than being swallowed.

These are the first ``async def test_`` functions in this repository.
``apps/api/pyproject.toml`` sets ``asyncio_mode = "auto"``, but that ini file
is never actually discovered when the documented command
(``PYTHONPATH=apps/api python3 -m pytest -q``) is run from the repository
root, since pytest's rootdir search walks upward from the invocation
directory and ``apps/api/`` is a subdirectory, not an ancestor, of the repo
root. pytest-asyncio therefore runs in its own default "strict" mode here,
which requires an explicit ``@pytest.mark.asyncio`` on every async test
regardless of that ini setting — hence the markers below. Pre-existing gap,
just never previously surfaced because no async test existed to hit it;
noted here rather than silently worked around, since a future async test
added without this comment would hit the same confusing error.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.providers.azure_openai_provider import AzureOpenAIProvider
from pydantic import BaseModel


class _Echo(BaseModel):
    value: str


class _FakeResponses:
    def __init__(self, output_text: str) -> None:
        self._output_text = output_text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self._output_text)


class _FakeAzureClient:
    def __init__(self, output_text: str) -> None:
        self.responses = _FakeResponses(output_text)


@pytest.mark.asyncio
async def test_structured_returns_parsed_response_model_with_mocked_client() -> None:
    fake_client = _FakeAzureClient(json.dumps({"value": "ok"}))
    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/",
        api_version="2024-10-21",
        deployment="gpt-4o-mini",
        client_factory=lambda: fake_client,
    )

    result = await provider.structured(prompt="hello", response_model=_Echo, purpose="test")

    assert result == _Echo(value="ok")
    assert fake_client.responses.calls[0]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_vision_structured_returns_parsed_response_model_with_mocked_client() -> None:
    fake_client = _FakeAzureClient(json.dumps({"value": "seen"}))
    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/",
        api_version="2024-10-21",
        deployment="gpt-4o-mini-vision",
        client_factory=lambda: fake_client,
    )

    result = await provider.vision_structured(
        prompt="describe", image_base64="Zm9v", response_model=_Echo, purpose="test",
    )

    assert result == _Echo(value="seen")
    call = fake_client.responses.calls[0]
    assert call["model"] == "gpt-4o-mini-vision"
    assert call["input"][0]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_missing_endpoint_raises_before_a_client_is_ever_built() -> None:
    built = []
    provider = AzureOpenAIProvider(
        endpoint=None,
        api_version="2024-10-21",
        deployment="gpt-4o-mini",
        client_factory=lambda: built.append("built") or _FakeAzureClient("{}"),
    )

    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        await provider.structured(prompt="hello", response_model=_Echo, purpose="test")

    assert built == []


@pytest.mark.asyncio
async def test_token_acquisition_failure_propagates_and_is_not_swallowed() -> None:
    def _raise_token_error():
        raise RuntimeError("failed to acquire an Azure AD token")

    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/",
        api_version="2024-10-21",
        deployment="gpt-4o-mini",
        client_factory=_raise_token_error,
    )

    with pytest.raises(RuntimeError, match="failed to acquire an Azure AD token"):
        await provider.structured(prompt="hello", response_model=_Echo, purpose="test")
