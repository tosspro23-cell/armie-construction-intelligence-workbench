from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, model: str, timeout_seconds: float = 90.0) -> None:
        self.api_key = api_key
        self.model = model
        # Independent review finding: this was never threaded through --
        # Settings.model_call_timeout_seconds existed but only
        # OllamaProvider actually respected it. openai's SDK default
        # timeout is 600s, far past this system's own request_timeout_seconds
        # (180s, config.py), so a stalled call here would never surface as
        # this provider's own timeout -- only the coarser, thread-level
        # outer deadline in main.py's chat() handler would eventually fire.
        self.timeout_seconds = timeout_seconds

    async def structured(self, *, prompt: str, response_model: type[T], purpose: str) -> T:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI provider calls.")
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout_seconds)
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
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI provider calls.")
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout_seconds)
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

