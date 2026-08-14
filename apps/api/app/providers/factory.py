from app.config import Settings
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider


def get_text_provider(settings: Settings):
    """Return only the provider actually selected for text interpretation."""
    if settings.llm_provider in {"ollama", "hybrid"}:
        return OllamaProvider(settings.ollama_base_url, settings.ollama_text_model, settings.model_call_timeout_seconds)
    return OpenAIProvider(settings.openai_api_key, settings.openai_text_model)


def get_vision_provider(settings: Settings):
    """Return only the provider selected for image-grounded reasoning."""
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings.ollama_base_url, settings.ollama_vision_model, settings.model_call_timeout_seconds)
    if settings.llm_provider == "hybrid":
        return OpenAIProvider(settings.openai_api_key, settings.openai_vision_model)
    return OpenAIProvider(settings.openai_api_key, settings.openai_vision_model)
