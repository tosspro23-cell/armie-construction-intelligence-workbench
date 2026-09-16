"""SPEC-M7: push-based indexing of the configured PDF corpus into Azure AI
Search -- no ADLS, no indexer/skillset pipeline (see the spec's Allowed
scope §B for why: disproportionate infrastructure for 18 single-page
documents). Whole-document text extraction (PyMuPDF), one embedding per
document (no chunking -- every document in this corpus is a single page),
idempotent upsert by a filename-derived id.

Run manually by an operator holding Search Index Data Contributor +
Search Service Contributor on the target search resource (not by the
running API, which only ever needs read access at query time -- see
D-018's RBAC least-privilege split). Requires AZURE_SEARCH_ENDPOINT,
AZURE_OPENAI_ENDPOINT (both env vars, matching apps/api/app/config.py's
Settings field names uppercased), and an authenticated `az login` session
(DefaultAzureCredential picks it up) with those two role assignments.

Usage:
    AZURE_SEARCH_ENDPOINT=https://armiem3-search.search.windows.net \
    AZURE_OPENAI_ENDPOINT=https://armie-m3-openai.openai.azure.com \
    PYTHONPATH=apps/api python3 scripts/index_document_corpus.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

DOCUMENT_TYPES = {
    "armie_demo_schedule.pdf": "schedule",
    "digitalhub_schedule.pdf": "schedule",
    "duplex_schedule.pdf": "schedule",
    "schedule_": "schedule",
    "digitalhub_schedule_": "schedule",
    "duplex_schedule_": "schedule",
    "door_spec_sheet": "spec_sheet",
    "window_spec_sheet": "spec_sheet",
    "digitalhub_door_spec_sheet": "spec_sheet",
    "digitalhub_window_spec_sheet": "spec_sheet",
    "duplex_door_spec_sheet": "spec_sheet",
    "duplex_window_spec_sheet": "spec_sheet",
    "rfi_log": "rfi",
    "digitalhub_rfi_log": "rfi",
    "duplex_rfi_log": "rfi",
    "meeting_minutes": "meeting_minutes",
    "digitalhub_meeting_minutes": "meeting_minutes",
    "duplex_meeting_minutes": "meeting_minutes",
}


def _document_type(filename: str) -> str:
    basename = Path(filename).name
    for prefix, doc_type in DOCUMENT_TYPES.items():
        if basename.startswith(prefix) or basename == prefix:
            return doc_type
    return "unknown"


def _extract_text(pdf_path: Path) -> str:
    import fitz

    document = fitz.open(pdf_path)
    return "\n".join(page.get_text() for page in document)


def _safe_id(filename: str) -> str:
    """Azure AI Search document keys allow letters, digits, `_`, `-`, `=`
    only -- filenames here contain `/` and `.` (e.g. "corpus/schedule_l2_
    east.pdf"), so this derives a stable, readable id rather than a bare
    hash, keeping index documents debuggable in the Azure Portal.
    """
    return filename.replace("/", "__").replace(".", "_")


def build_index(index_client, index_name: str, vector_dimensions: int) -> None:
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="filename", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="document_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True, vector_search_dimensions=vector_dimensions, vector_search_profile_name="default-hnsw",
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw-algorithm")],
        profiles=[VectorSearchProfile(name="default-hnsw", algorithm_configuration_name="default-hnsw-algorithm")],
    )
    index_client.create_or_update_index(SearchIndex(name=index_name, fields=fields, vector_search=vector_search))


async def _embed_with_retry(embedding_provider, text: str, pdf_file: str, max_attempts: int = 6):
    """SPEC-M13: found live indexing 35 documents (up from the original 18)
    against `armiem3-openai`'s `text-embedding-3-small` deployment
    (S0/GlobalStandard, capacity 1) -- the deployment's own low
    requests-per-minute ceiling was exceeded partway through the batch,
    something the original 18-document corpus never triggered. Azure
    OpenAI's `RateLimitError` reports its own suggested wait via the
    `Retry-After` header; honored directly here instead of a blind fixed
    sleep, with an exponential-backoff fallback if that header is absent.
    A `RateLimitError` immediately followed by an `APITimeoutError` was
    also observed live (the sustained rate-limited state appears to leave
    the connection in a bad state for one more call) -- retried the same
    way, with a fixed short wait since it is not a quota signal.
    """
    import openai

    for attempt in range(max_attempts):
        try:
            return await embedding_provider.embed(text)
        except openai.RateLimitError as error:
            if attempt == max_attempts - 1:
                raise
            retry_after = getattr(getattr(error, "response", None), "headers", {}).get("Retry-After")
            wait_seconds = float(retry_after) if retry_after else 2 ** attempt * 5
            print(f"  rate limited embedding {pdf_file} -- waiting {wait_seconds:.0f}s (attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(wait_seconds)
        except openai.APITimeoutError:
            if attempt == max_attempts - 1:
                raise
            print(f"  timed out embedding {pdf_file} -- waiting 15s (attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(15)


async def index_corpus(pdf_files: list[str]) -> None:
    from app.config import get_settings
    from app.providers.factory import get_embedding_provider
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    settings = get_settings()
    if not settings.azure_search_endpoint:
        raise SystemExit("AZURE_SEARCH_ENDPOINT is not set -- nothing to index against.")
    embedding_provider = get_embedding_provider(settings)
    if embedding_provider is None:
        raise SystemExit("Azure AI Search is configured but no embedding provider resolved -- check azure_search_endpoint/azure_openai_endpoint.")

    credential = DefaultAzureCredential()
    index_client = SearchIndexClient(endpoint=settings.azure_search_endpoint, credential=credential)
    search_client = SearchClient(endpoint=settings.azure_search_endpoint, index_name=settings.azure_search_index_name, credential=credential)

    documents = []
    for index, pdf_file in enumerate(pdf_files):
        path = settings.data_dir / pdf_file
        if not path.exists():
            print(f"skip (missing): {pdf_file}")
            continue
        # SPEC-M13: found live -- even with `_embed_with_retry`'s backoff,
        # firing every embed call back-to-back re-triggered this
        # deployment's (S0/GlobalStandard, capacity 1) rate limit almost
        # immediately after each wait, since nothing paced the *successful*
        # calls, only the failed ones. A fixed gap between every call,
        # not just after a 429, keeps sustained throughput under whatever
        # this tier's real ceiling is instead of bursting into it.
        if index > 0:
            await asyncio.sleep(20)
        text = _extract_text(path)
        vector = await _embed_with_retry(embedding_provider, text, pdf_file)
        # `filename` stores the bare basename, not the pdf_files-relative
        # path (e.g. "schedule_l2_east.pdf", not "corpus/schedule_l2_east.
        # pdf") -- this must match ServiceContainer.document_analyzers'
        # keys exactly (also basenames, see services.py), since
        # _retrieve_relevant_documents (graph.py) filters retrieved
        # filenames against that dict. A real bug caught empirically, not
        # assumed: an end-to-end TestClient run against the live index
        # returned zero retrieval candidates until this was fixed, because
        # every retrieved filename was silently excluded by that filter.
        basename = Path(pdf_file).name
        documents.append({
            "id": _safe_id(basename), "filename": basename,
            "document_type": _document_type(pdf_file), "content": text, "content_vector": vector,
        })
        print(f"embedded: {pdf_file} ({len(text)} chars, {len(vector)}-dim vector)")

    if not documents:
        raise SystemExit("No documents to index -- check pdf_files/data_dir.")

    build_index(index_client, settings.azure_search_index_name, vector_dimensions=len(documents[0]["content_vector"]))
    result = search_client.upload_documents(documents=documents)
    failed = [r for r in result if not r.succeeded]
    print(f"indexed {len(documents) - len(failed)}/{len(documents)} documents into '{settings.azure_search_index_name}'")
    if failed:
        for r in failed:
            print(f"  FAILED: {r.key} -- {r.error_message}")
        raise SystemExit(1)


if __name__ == "__main__":
    # The full corpus across every project that has one (SPEC-M6's 18 demo
    # documents plus SPEC-M13's 7-per-building DigitalHub/Duplex corpora),
    # independent of whatever a given deployment's PDF_FILES happens to be
    # set to -- indexing is an operator action against the fixture, not
    # something that should silently track a runtime setting meant for
    # something else. One shared index (see this file's own header) means
    # one shared indexing entry point, not a per-project script.
    full_corpus = [
        "armie_demo_schedule.pdf",
        *[f"corpus/{name}" for name in [
            "schedule_l2_east.pdf", "schedule_l2_west.pdf", "schedule_l3_east.pdf", "schedule_l3_west.pdf",
            "schedule_mezzanine.pdf", "schedule_roof_plant.pdf",
            "door_spec_sheet_package_a.pdf", "door_spec_sheet_package_b.pdf",
            "window_spec_sheet_package_a.pdf", "window_spec_sheet_package_b.pdf",
            "rfi_log_047.pdf", "rfi_log_052.pdf", "rfi_log_058.pdf", "rfi_log_061.pdf",
            "meeting_minutes_2026_02_10.pdf", "meeting_minutes_2026_02_24.pdf", "meeting_minutes_2026_03_10.pdf",
        ]],
        "projects/digitalhub/digitalhub_schedule.pdf",
        *[f"projects/digitalhub/corpus/{name}" for name in [
            "digitalhub_schedule_b01.pdf", "digitalhub_schedule_e00.pdf", "digitalhub_schedule_e01.pdf",
            "digitalhub_door_spec_sheet.pdf", "digitalhub_window_spec_sheet.pdf",
            "digitalhub_rfi_log_001.pdf", "digitalhub_meeting_minutes_2026_02_10.pdf",
        ]],
        "projects/duplex/duplex_schedule.pdf",
        *[f"projects/duplex/corpus/{name}" for name in [
            "duplex_schedule_level1.pdf", "duplex_schedule_level2.pdf", "duplex_schedule_roof.pdf",
            "duplex_door_spec_sheet.pdf", "duplex_window_spec_sheet.pdf",
            "duplex_rfi_log_001.pdf", "duplex_meeting_minutes_2026_02_10.pdf",
        ]],
    ]
    asyncio.run(index_corpus(full_corpus))
