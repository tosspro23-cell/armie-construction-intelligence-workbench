from app.config import Settings
from app.providers.base import ModelProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider


def get_text_provider(settings: Settings) -> ModelProvider:
    """Return only the provider actually selected for text interpretation."""
    if settings.llm_provider in {"ollama", "hybrid"}:
        return OllamaProvider(settings.ollama_base_url, settings.ollama_text_model, settings.model_call_timeout_seconds)
    return OpenAIProvider(settings.openai_api_key, settings.openai_text_model)


def get_vision_provider(settings: Settings) -> ModelProvider:
    """Return only the provider selected for image-grounded reasoning."""
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings.ollama_base_url, settings.ollama_vision_model, settings.model_call_timeout_seconds)
    if settings.llm_provider == "hybrid":
        return OpenAIProvider(settings.openai_api_key, settings.openai_vision_model)
    return OpenAIProvider(settings.openai_api_key, settings.openai_vision_model)


def get_escalation_provider(settings: Settings) -> ModelProvider | None:
    """Return the configured bounded semantic-repair escalation provider.

    Escalation is opt-in (SPEC-M1 §4.2/9, OD-2): returns ``None`` unless
    ``ollama_escalation_model`` is explicitly configured, so no environment
    is silently required to hold a large local model.
    """
    if not settings.ollama_escalation_model:
        return None
    return OllamaProvider(settings.ollama_base_url, settings.ollama_escalation_model, settings.model_call_timeout_seconds)
