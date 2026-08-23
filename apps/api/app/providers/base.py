from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


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

