from __future__ import annotations

from typing import TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    def __init__(self, message: str, *, raw_response: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.status_code = status_code


def _extract_json(content: str | None) -> str:
    if not content:
        raise StructuredOutputError("The model returned an empty structured response.", raw_response=content)
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    raise StructuredOutputError("No JSON object could be extracted from the model response.", raw_response=content)


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def structured(self, *, prompt: str, response_model: type[T], purpose: str) -> T:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # Semantic planning needs concise typed output, not an exposed long
            # reasoning trace. Qwen3 supports disabling think mode here.
            "think": False,
            "format": response_model.model_json_schema(),
            "options": {"temperature": 0},
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            try:
                response.raise_for_status()
            except Exception as error:
                raise StructuredOutputError(str(error), status_code=response.status_code) from error
        body = response.json()
        # Some Qwen VL builds place schema-constrained content in ``thinking``
        # while leaving ``response`` empty. Accept that provider-specific
        # transport detail, then still validate the exact Pydantic contract.
        content = body.get("response") or body.get("thinking")
        try:
            return response_model.model_validate_json(_extract_json(content))
        except StructuredOutputError:
            raise
        except Exception as error:
            raise StructuredOutputError(str(error), raw_response=content) from error

    async def vision_structured(
        self, *, prompt: str, image_base64: str, response_model: type[T], purpose: str
    ) -> T:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [image_base64],
            "stream": False,
            "think": False,
            "format": response_model.model_json_schema(),
            "options": {"temperature": 0},
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
        body = response.json()
        content = body.get("response") or body.get("thinking")
        return response_model.model_validate_json(_extract_json(content))
