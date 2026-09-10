# M7 — Azure AI Search Retrieval Baseline (live, real evidence)

Owner-authorized run against the same Azure subscription as the M3/M4/M5/M6
baselines, 2026-09-10. Records real, measured evidence for SPEC-M7's central
claim and for `azure_search_relevance_threshold`'s value -- neither was assumed
or guessed, per the owner's explicit instruction this milestone: conclusions
come from real data against the live service, not narrative.

## What was deployed

- `armiem3-search` (Azure AI Search, `sku: free`, `centralus` -- `eastus2`, this
  subscription's resource-group region, was out of capacity, the same
  restriction D-014 hit for Postgres). Created during this session's
  cost-feasibility check and kept for direct use (OD-34).
- `disableLocalAuth: true` set on `armiem3-search` -- no API-key auth path
  exists at all, Managed Identity/AAD only.
- RBAC: the API's runtime identity (`armiem3-identity`) granted **Search Index
  Data Reader** only (query, least privilege); the operator identity (this
  session's own `az login`) granted **Search Index Data Contributor** +
  **Search Service Contributor** (index/schema management, for the indexing
  script) and **Cognitive Services OpenAI User** on `armie-m3-openai` (to call
  the new embedding deployment directly while indexing).
- `text-embedding-3-small` deployed on the existing `armie-m3-openai` resource
  (`GlobalStandard`, capacity 1) -- no new Azure OpenAI resource.
- `armie-document-corpus` index built and populated via `scripts/
  index_document_corpus.py` (push-based, no ADLS/indexer pipeline, per the
  spec's §B): all 18 SPEC-M6 corpus documents, whole-document text (PyMuPDF),
  one embedding per document, `filename` field storing the bare basename
  (matching `ServiceContainer.document_analyzers`' keys -- see "Defect found
  and fixed" below).

## RBAC propagation, observed directly

Granting "Cognitive Services OpenAI User" to the operator identity did not
take effect immediately: the exact same embedding call failed with `401
PermissionDenied` for roughly 8-10 minutes after the role assignment, then
succeeded with no code or configuration change. Confirmed this was
propagation, not a wrong role/scope, by decoding the AAD token directly
(`oid`/`aud` claims were already correct on every attempt) and by a raw `az
account get-access-token` + `curl` REST call succeeding independently before
the SDK path did. Not a new finding specific to this project, but recorded
because it cost real wall-clock time and could look like a code bug otherwise.

## Defect found and fixed (live, not caught by any unit test)

The first indexing run stored each document's `filename` field as the
`Settings.pdf_files`-relative path (e.g. `"corpus/schedule_l2_east.pdf"`).
`ServiceContainer.document_analyzers` (SPEC-M6) keys its dict by the bare
basename (`"schedule_l2_east.pdf"`). `_retrieve_relevant_documents`
(`app/agent/graph.py`) filters retrieved filenames against that dict --
with the mismatch, every retrieval candidate was silently excluded, and a
live `TestClient` run against the real index returned the *unchanged* blanket
miss message instead of a directed one. Found only by actually running the
end-to-end HTTP path against the live index, not by the CI-safe fake-client
unit tests (which inject filenames that already matched, by construction, and
so could not have caught a real indexing-side bug). Fixed in `scripts/
index_document_corpus.py` (store `Path(pdf_file).name`, not the relative
path); the index was deleted and rebuilt from scratch rather than patched in
place, since 18 documents cost nothing to regenerate and a partial migration
would have left both old- and new-scheme ids coexisting.

## Central claim: does retrieval correctly rank the recall-failure document?

Query: `"What is the connected load for Panel-E?"` (SPEC-M6's exact
recall-failure fixture question) against the real index, hybrid search
(BM25 + vector):

```
0.03280  corpus/rfi_log_047.pdf         <- correct, ranks #1
0.03154  corpus/schedule_l2_east.pdf
0.03095  corpus/schedule_mezzanine.pdf
0.03058  corpus/schedule_l3_east.pdf
0.03055  corpus/schedule_l2_west.pdf
0.01639  corpus/rfi_log_058.pdf
0.01613  corpus/meeting_minutes_2026_03_10.pdf
0.01587  corpus/meeting_minutes_2026_02_10.pdf
```

`rfi_log_047.pdf` ranks first. **Confirmed end-to-end**, not just at the raw
Azure AI Search API level: a real `TestClient` request against
`/api/v1/chat` with `PDF_FILES=["corpus/schedule_l2_east.pdf",
"corpus/schedule_l2_west.pdf", "corpus/rfi_log_047.pdf"]` and this same
question returned:

```
disposition: clarification_required
answer_markdown: "I could not automatically extract this field, but the
  most relevant configured document(s) may be: rfi_log_047.pdf,
  schedule_l2_east.pdf, schedule_l2_west.pdf."
model_call_count: 1
```

-- naming the RFI first (the SDK's own ranked order, preserved), instead of
SPEC-M6's blanket "I could not find this field with a confident match in any
configured document."

**The precision-failure (collision) case is unaffected, verified by call-count,
not just by the answer.** The same live run against `"What is the connected
load for Panel-A?"` returned the identical SPEC-M6 response
(`clarification_required`, naming both `schedule_l2_east.pdf`/
`schedule_l2_west.pdf`) with `model_call_count: 0` -- retrieval was never
invoked, because `native_lookup` already found two confident hits and the
zero-hit branch (the only place retrieval runs) was never reached.

## Honest nuance, checked rather than assumed: what actually resolves the recall failure

Re-running the Panel-E query as **vector-only** (no BM25 text contribution)
produced a different, important result:

```
0.68653  corpus/schedule_mezzanine.pdf   <- NOT the RFI
0.68508  corpus/schedule_l2_east.pdf
0.68367  corpus/rfi_log_047.pdf          <- ranks 3rd on pure vector similarity
0.68189  corpus/schedule_l3_east.pdf
0.67818  corpus/schedule_l2_west.pdf
```

On the exact SPEC-M6 fixture question, pure vector similarity does **not**
rank the RFI first -- hybrid's success here is substantially attributable to
BM25's exact-term match on "Panel-E," which appears literally in the RFI's
prose. That is a real, useful outcome (a full-text index that hybrid search
happens to include resolves the fixture's literal recall gap), but it is a
weaker claim than "semantic embeddings understood the vocabulary mismatch" --
mechanically, any full-text search over whole-document text would likely have
found this passage via the shared literal token, independent of embeddings.

A further, deliberately harder test -- a paraphrase that avoids the literal
name "Panel-E" entirely (`"How much electrical capacity was allocated to the
new mezzanine distribution panel added by change request?"`) -- isolates the
genuine semantic-similarity claim:

```
vector-only:
0.78679  corpus/rfi_log_047.pdf          <- correct, clear margin
0.70755  corpus/schedule_mezzanine.pdf
0.69628  corpus/schedule_l3_east.pdf
```

Here, pure vector search ranks the RFI first by a wide, unambiguous margin
(0.787 vs. 0.708 runner-up) with zero literal token overlap between the
question and the answer's vocabulary ("connected capacity" / "mezzanine
distribution panel" vs. the question's own paraphrase) -- this is the
actually-semantic version of SPEC-M6's recall-failure claim. **Conclusion,
stated plainly rather than oversold:** hybrid search (the shipped
configuration) correctly resolves SPEC-M6's exact fixture question, but a
meaningful share of that specific win is attributable to lexical overlap on
the entity name, not embedding semantics alone; the harder paraphrased case
confirms the semantic-similarity claim independently holds when literal
overlap is removed. Both are real, both are evidence; they are not the same
claim, and this report does not conflate them.

## `azure_search_relevance_threshold` (OD-35), set from measured data

Every query tried against the live index showed the same clear, bimodal
split: topically relevant documents (schedules discussing panels/loads, the
planted RFI) scored **0.030-0.033**; clearly irrelevant documents (meeting
minutes, off-topic RFIs) scored **0.014-0.019**, across every question tested
above. `0.025` (`apps/api/app/config.py`) sits in the gap between those two
observed clusters -- not a round-number guess, and not tuned against a single
query in isolation.

## Cost posture

No new billable resources: `armiem3-search` is `sku: free` ($0). The
embedding deployment (`text-embedding-3-small`, `GlobalStandard`, capacity 1)
bills per-token, consistent with the existing `gpt-5-mini` deployment's
pricing model; indexing 18 short single-page documents and the handful of
diagnostic queries in this report cost a negligible, sub-cent amount of
embedding tokens.
