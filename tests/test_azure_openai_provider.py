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


class _FakeCompletions:
    """Mimics ``client.chat.completions``, not the Responses API (SPEC-M3's
    first real deployment found the Responses API route returns 404 on a
    real Azure OpenAI resource; Chat Completions is the surface both
    OpenAIProvider and AzureOpenAIProvider now use -- see
    azure_openai_provider.py's docstring for the live-verified evidence).
    """

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)


class _FakeAzureClient:
    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)


def _rate_limit_error(retry_after: str = "0.01") -> Exception:
    """A real `openai.RateLimitError`, not a stand-in -- the retry helper
    in `azure_openai_provider.py` matches on this exact exception class."""
    import httpx
    import openai

    response = httpx.Response(429, headers={"retry-after": retry_after}, request=httpx.Request("POST", "https://fake.openai.azure.com/"))
    return openai.RateLimitError("rate limit exceeded", response=response, body=None)


class _StreamChunk:
    def __init__(self, content: str | None = None, finish_reason: str | None = None) -> None:
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None), finish_reason=finish_reason)]


class _FakeStreamCompletions:
    """Mimics `client.chat.completions.create(stream=True)` -- raises a
    real `openai.RateLimitError` on the first `fail_times` calls, then
    returns a minimal real streamed answer."""

    def __init__(self, fail_times: int, retry_after: str = "0.01") -> None:
        self.fail_times = fail_times
        self.retry_after = retry_after
        self.call_count = 0

    async def create(self, **kwargs):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise _rate_limit_error(self.retry_after)

        async def _chunks():
            yield _StreamChunk(content="ok")
            yield _StreamChunk(finish_reason="stop")

        return _chunks()


class _FakeStreamChat:
    def __init__(self, completions: _FakeStreamCompletions) -> None:
        self.completions = completions


class _FakeStreamAzureClient:
    def __init__(self, fail_times: int, retry_after: str = "0.01") -> None:
        self.completions = _FakeStreamCompletions(fail_times, retry_after)
        self.chat = _FakeStreamChat(self.completions)


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
    assert fake_client.chat.completions.calls[0]["model"] == "gpt-4o-mini"


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
    call = fake_client.chat.completions.calls[0]
    assert call["model"] == "gpt-4o-mini-vision"
    assert call["messages"][0]["content"][1]["type"] == "image_url"


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


@pytest.mark.asyncio
async def test_stream_turn_retries_a_transient_rate_limit_and_then_succeeds() -> None:
    """D-067 (2026-09-21): found live via an owner-requested stress test --
    a real Azure OpenAI 429 on `stream_turn` previously surfaced
    immediately as `disposition=error`, with no retry attempted at all.
    This is the ordinary, ok-to-retry case: a short-lived burst that
    clears on its own. One failure, then a real success -- must not be
    visible to the caller as an error at all.
    """
    fake_client = _FakeStreamAzureClient(fail_times=1, retry_after="0.01")
    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/", api_version="2024-10-21", deployment="gpt-5-mini",
        client_factory=lambda: fake_client,
    )

    events = [event async for event in provider.stream_turn(messages=[{"role": "user", "content": "hi"}], tools=[], purpose="test")]

    assert fake_client.completions.call_count == 2  # one failed attempt, one real success
    answer_chunks = [event for event in events if type(event).__name__ == "AnswerChunkEvent"]
    assert any(chunk.text == "ok" for chunk in answer_chunks)


@pytest.mark.asyncio
async def test_stream_turn_gives_up_after_repeated_rate_limits_and_raises() -> None:
    """The complementary case: a 429 that never clears (this project's own
    real incident -- a request too *large* to ever fit the budget, not a
    passing burst) must still surface as a real failure once retries are
    exhausted, not retry forever or fail silently.
    """
    fake_client = _FakeStreamAzureClient(fail_times=99, retry_after="0.01")
    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/", api_version="2024-10-21", deployment="gpt-5-mini",
        client_factory=lambda: fake_client,
    )

    import openai

    with pytest.raises(openai.RateLimitError):
        async for _event in provider.stream_turn(messages=[{"role": "user", "content": "hi"}], tools=[], purpose="test"):
            pass

    # Exactly the original attempt plus the configured retry budget -- not
    # an unbounded loop.
    from app.providers.azure_openai_provider import _MAX_RATE_LIMIT_RETRIES

    assert fake_client.completions.call_count == _MAX_RATE_LIMIT_RETRIES + 1


@pytest.mark.asyncio
async def test_stream_turn_honors_the_servers_own_retry_after_header() -> None:
    """Owner's own `Retry-After` guidance is used instead of a guessed
    fixed delay -- verified by patching `asyncio.sleep` and asserting it
    was actually called with the header's value, not the module's
    fallback default.
    """
    from unittest.mock import AsyncMock, patch

    fake_client = _FakeStreamAzureClient(fail_times=1, retry_after="2.5")
    provider = AzureOpenAIProvider(
        endpoint="https://fake.openai.azure.com/", api_version="2024-10-21", deployment="gpt-5-mini",
        client_factory=lambda: fake_client,
    )

    with patch("app.providers.azure_openai_provider.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        events = [event async for event in provider.stream_turn(messages=[{"role": "user", "content": "hi"}], tools=[], purpose="test")]

    assert events  # the retry succeeded
    mock_sleep.assert_awaited_once_with(2.5)
