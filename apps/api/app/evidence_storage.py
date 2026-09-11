from __future__ import annotations

from app.config import Settings


def get_blob_container_client(settings: Settings) -> object | None:
    """Opt-in (SPEC-M8): returns None unless
    `evidence_storage_account_url` is set -- no environment is required to
    reach a storage account just to run this project, the same pattern as
    `database_url`/`azure_search_endpoint`.

    Managed Identity only (OD-23 extended, D-020): the real
    `armiem3evidence` storage account has `allowSharedKeyAccess: false`
    set, so no API-key/connection-string code path exists here at all --
    `DefaultAzureCredential` is the only credential mechanism.

    Returns `object`, not a typed `ContainerClient`, so importing this
    module never requires `azure-storage-blob` to be installed unless
    this function is actually called -- consistent with
    `app/retrieval.py`'s own lazy-import discipline.
    """
    if not settings.evidence_storage_account_url:
        return None
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import ContainerClient

    return ContainerClient(
        account_url=settings.evidence_storage_account_url,
        container_name=settings.evidence_storage_container_name,
        credential=DefaultAzureCredential(),
    )
