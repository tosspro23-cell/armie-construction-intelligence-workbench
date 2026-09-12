from __future__ import annotations

from app.config import Settings


class DataLakeClient:
    """Thin wrapper around `azure-storage-file-datalake` (SPEC-M9).

    Exposes exactly one operation -- `read_file(path) -> bytes` -- so
    `ServiceContainer`'s per-project download logic (services.py) can be
    exercised in tests against a fake object with this same interface (D-007
    discipline, mirroring `app/retrieval.py`'s `search_client_factory` and
    `app/evidence_storage.py`'s `get_blob_container_client` seams), instead
    of mocking the real SDK's nested filesystem/file client hierarchy.
    """

    def __init__(self, service_client: object, filesystem_name: str) -> None:
        self._filesystem = service_client.get_file_system_client(filesystem_name)

    def read_file(self, path: str) -> bytes:
        file_client = self._filesystem.get_file_client(path)
        downloader = file_client.download_file()
        return downloader.readall()


def get_datalake_client(settings: Settings) -> DataLakeClient | None:
    """Opt-in (SPEC-M9): returns `None` unless `adls_account_url` is set --
    no environment is required to reach a Data Lake account just to run
    this project, the same pattern as every other Azure setting.

    Managed Identity only (OD-23 extended, D-023): no API-key/connection-
    string code path exists here at all -- `DefaultAzureCredential` is the
    only credential mechanism, matching every other Azure resource in this
    project.

    Returns a `DataLakeClient`, not the raw SDK client, so importing this
    module never requires `azure-storage-file-datalake` to be installed
    unless this function is actually called -- consistent with
    `app/retrieval.py`'s own lazy-import discipline.
    """
    if not settings.adls_account_url:
        return None
    from azure.identity import DefaultAzureCredential
    from azure.storage.filedatalake import DataLakeServiceClient

    service_client = DataLakeServiceClient(
        account_url=settings.adls_account_url,
        credential=DefaultAzureCredential(),
    )
    return DataLakeClient(service_client, settings.adls_filesystem_name)
