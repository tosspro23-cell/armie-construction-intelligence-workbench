from __future__ import annotations

from app.config import Settings


def get_search_client(settings: Settings) -> object | None:
    """Opt-in (SPEC-M7): returns None unless `azure_search_endpoint` is set
    -- no environment is required to reach a search service just to run
    this project, the same pattern as `database_url`/`api_shared_secret`.

    Managed Identity only (OD-23 extended, D-018): `armiem3-search` itself
    has `disableLocalAuth: true` set, so no API-key code path exists here
    at all -- `DefaultAzureCredential` is the only credential mechanism,
    matching `AzureOpenAIProvider`'s existing posture.

    Returns `object`, not a typed `SearchClient`, so importing this module
    never requires `azure-search-documents` to be installed unless this
    function is actually called -- consistent with how
    `AzureOpenAIProvider`'s client builder imports `openai`/`azure.identity`
    lazily inside the function body, not at module load time.
    """
    if not settings.azure_search_endpoint:
        return None
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient

    return SearchClient(
        endpoint=settings.azure_search_endpoint,
        index_name=settings.azure_search_index_name,
        credential=DefaultAzureCredential(),
    )
