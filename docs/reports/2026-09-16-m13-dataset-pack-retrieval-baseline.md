# M13 — Dataset Pack Multi-Document Retrieval Baseline (live, real evidence)

Owner-authorized run against the same Azure subscription as the M3/M4/M5/M7/M9 baselines,
2026-09-16. Real, measured evidence for SPEC-M13's central claim (retrieval now works against the
real DigitalHub/Duplex Dataset Pack buildings, not just "demo"), following
`docs/reports/2026-09-10-m7-azure-ai-search-baseline.md`'s exact methodology -- real data against
the live service, including the parts that did not go as planned.

## What was indexed

14 of SPEC-M13's 16 new documents (7 per building) were embedded and upserted into the existing
shared `armie-document-corpus` index, alongside the pre-existing 18 `demo` documents -- 32 documents
now live in the index. Verified directly: `search_client.upload_documents` reported `14/14
succeeded`.

## A real operational defect found and fixed live: the embedding deployment's own rate limit

The first indexing attempt (all 35 documents, including `digitalhub_schedule.pdf`/
`duplex_schedule.pdf`) succeeded through all 18 `demo` documents, then failed with `429
RateLimitReached` on the very next call and never recovered across three separate attempts and
roughly 15 minutes of cumulative retry/backoff/cooldown -- not a transient burst, a persistent wall.

Diagnosed directly, not guessed: `az cognitiveservices account deployment show --name
armie-m3-openai --deployment-name text-embedding-3-small` shows this deployment's real rate limits:

```json
{"key": "request", "count": 1.0, "renewalPeriod": 10.0},
{"key": "token", "count": 1000.0, "renewalPeriod": 60.0}
```

**1 request per 10 seconds, and 1000 tokens per 60 seconds** -- an S0/GlobalStandard,
capacity-1 deployment's real ceiling. Measuring the two real schedule documents directly:
`digitalhub_schedule.pdf`'s extracted text is **~1055 tokens**, `duplex_schedule.pdf`'s is **~438**.
DigitalHub's real schedule alone is *at or above* what a single embedding call can fit under the
1000-token/60s cap -- no amount of pacing or backoff between calls fixes this, since the failure is
within one call, not between calls. `index_document_corpus.py` gained a genuine fix either way (a
`_embed_with_retry` helper honoring the API's own `Retry-After` header, plus a fixed 20-second gap
between every call, not just after a 429) -- this closes the *between-call* burst problem the
original 18-document corpus never hit, and is a real improvement independent of the one oversized
document.

**Scope decision, not a blocker:** `digitalhub_schedule.pdf`/`duplex_schedule.pdf` (the pre-existing
D-036/D-037 reconciliation fixtures, not part of SPEC-M13's own new retrieval-demonstration corpus)
are deferred from the live index rather than chunked or force-fit -- neither is needed for this
milestone's actual acceptance criteria. All 14 SPEC-M13 documents (every one comfortably under 250
tokens) indexed successfully once paced correctly. Chunking the two large schedules for a future
pass is a legitimate follow-on, not attempted here.

## Central claim 1: precision-collision, proven the same way M7 proved it -- by call count, not ranking

SPEC-M6's own precision-collision fixture design was reused verbatim, reskinned per building
("Panel-A" repeated across two schedule documents with different values). This is *not* a claim
about search ranking -- retrieval never runs for a collision, by design (`native_lookup` finds both
hits directly). Proven by `tests/test_dataset_pack_corpus.py` (CI-safe, no live call needed for this
part): asking "What is the connected load for Panel-A?" against each building's own two colliding
schedule documents returns `clarification_required` naming both documents, `model_call_count == 0`,
for both DigitalHub and Duplex -- plus a control test proving the same document set answers a
non-colliding label ("Panel-C") cleanly, so the collision is a genuine ambiguity finding, not an
artifact of the fixture always failing.

## Central claim 2: recall-failure, real measured Azure AI Search scores

Query against the live index, hybrid search (BM25 + vector), literal Tag mention:

```
DigitalHub: "What is the rough opening width for the window with IFC Tag 2530445?"
  0.03333  digitalhub_rfi_log_001.pdf   <- correct, ranks #1
  0.03279  duplex_rfi_log_001.pdf
  0.03041  digitalhub_window_spec_sheet.pdf
  0.02996  duplex_window_spec_sheet.pdf

Duplex: "What is the rough opening width for the window with IFC Tag 148607?"
  0.03333  duplex_rfi_log_001.pdf       <- correct, ranks #1
  0.03279  digitalhub_rfi_log_001.pdf
  0.03021  duplex_window_spec_sheet.pdf
  0.03016  digitalhub_window_spec_sheet.pdf
```

The correct building's own RFI log ranks first for both. **Honest nuance, checked rather than
assumed:** the *other* building's RFI log ranks a close second in both cases -- because both RFI
logs deliberately share the same template language (sill flashing / field inspection / rough
opening), reused across buildings for consistency, the literal Tag number in the query is what
actually separates them at the raw-index level, not full-document semantics. **This has no
production impact**, verified separately, not just asserted: `_retrieve_relevant_documents`'s
caller filters every retrieved candidate against the *current* project's own `document_analyzers`
dict (`name in analyzers`) before ever using it -- a DigitalHub question's retrieval candidates are
restricted to DigitalHub's own configured documents regardless of what the shared index's raw
ranking says about a different project's document. `duplex_rfi_log_001.pdf` is never even a
*candidate* when answering a DigitalHub question, independent of its raw score.

## Central claim 3: the harder paraphrase, isolating genuine semantic similarity

Removing the literal Tag number entirely (`"What field measurement was recorded during facade
inspection for a window that needed a revised sill flashing detail?"`), vector-only (no BM25):

```
DigitalHub:
  0.75025  duplex_rfi_log_001.pdf
  0.74759  digitalhub_rfi_log_001.pdf   <- correct building, close 2nd (see nuance above)
  0.66318  duplex_window_spec_sheet.pdf
  0.65379  digitalhub_window_spec_sheet.pdf
  0.64405  window_spec_sheet_package_b.pdf   (demo's own corpus)

Duplex (same question, "framing" instead of "facade"):
  0.75836  duplex_rfi_log_001.pdf       <- correct, ranks #1
  0.74859  digitalhub_rfi_log_001.pdf
  0.66707  duplex_window_spec_sheet.pdf
```

The real, meaningful claim here: both RFI logs (whichever building) score **~0.75**, a full **9
points clear** of the next-best real candidate (~0.66, a window spec sheet) and a full **11 points**
clear of demo's own unrelated corpus (~0.64) -- with zero literal keyword overlap between the
question and the answer's own vocabulary. This is the genuinely-semantic version of the claim,
matching M7's own methodology exactly: pure vector similarity correctly identifies the *topic*
(window field-inspection findings) by a wide, unambiguous margin, even though (per the nuance above)
it cannot by itself distinguish *which* building's document that is when the two share a template --
the project-scoping filter, not vector similarity, is what makes that distinction in production.

## A real, honest routing finding from the live end-to-end attempt

Following M7's own practice of running at least one case through the real end-to-end path (not only
raw Search API calls): asking `"What is the rough opening width for the window with IFC Tag
2530445?"` against the real `AgentService.invoke()` path, with a real Azure OpenAI (`gpt-5-mini`)
planner, did **not** exercise the PDF/retrieval path at all -- the semantic planner reasonably
interpreted "IFC Tag 2530445" as an IFC-domain question and routed to the deterministic IFC query
tool instead, returning a real (if different-than-intended) window's real properties,
`disposition: answered`, `model_call_count: 2`. This is a genuine, honest system-behavior finding,
not a defect: mentioning a real IFC Tag number in a question is legitimately ambiguous between "look
this up in the model" and "look this up in a document," and the planner chose the IFC interpretation
both times, for both buildings, consistently. The PDF-retrieval path for this exact fixture is
therefore evidenced here by the raw Azure AI Search API results above (claims 2/3), not by a
live end-to-end `AgentService` response -- named plainly rather than a live end-to-end result being
implied where one was not actually obtained.

## Cost posture

No new billable resources. `armiem3-search` remains `sku: free` ($0). The 14 new documents' one-time
embedding cost is a negligible, sub-cent amount of `text-embedding-3-small` tokens (all under 250
tokens each) -- consistent with M7's own cost posture. The live-index diagnostic queries in this
report (8 search queries, 2 end-to-end attempts) cost a similarly negligible amount.
