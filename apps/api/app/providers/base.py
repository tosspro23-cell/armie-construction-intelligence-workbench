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

