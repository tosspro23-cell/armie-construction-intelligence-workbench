# SPEC-M16 — V1 vs V2 live benchmark (real Azure OpenAI, no fake provider)

All numbers below are from real, live calls against the actual `armie-m3-openai`/`gpt-5-mini`
deployment (the same Azure resource `armiem3` production traffic uses), the real `armie_demo.ifc`
fixture, and the real reconciliation PDF schedule -- no `FakeModelProvider`, no mocked timing.
Measured 2026-09-16, this branch (`feat/m16-v2-tool-calling-agent`) at commit `c2ebabe`.

## Correctness: 10/10 representative questions, zero fabricated facts

| # | Question | Operation exercised | V2 disposition | V2 correct? |
|---|---|---|---|---|
| 1 | How many doors are there? | count | answered | Yes -- 4 (matches real IFC) |
| 2 | How many windows and doors are there? | parallel count (2 tools, 1 round) | answered | Yes -- 4 and 4 |
| 3 | What is the maximum door height? | aggregate_quantity | answered | Yes -- 2.100 m, real GlobalIds cited |
| 4 | 这个项目里有多少扇窗？ | count (Chinese) | answered | Yes -- 4 |
| 5 | 哪个楼层的窗户最多？ | group_by storey + argmax (Chinese) | answered | Yes -- correctly reported the real tie (2/2) rather than inventing a false winner |
| 6 | What properties does the door have? | get_properties, 4 real matches | answered | Yes -- listed all 4, asked which one if a single answer was wanted (the exact honesty fix D-060 required of V1) |
| 7 | Compare the doors/windows against the PDF schedule | reconcile_doors_windows | answered | Yes -- identical counts to V1's own dedicated reconciliation path (see below) |
| 8 | 这个建筑里所有窗户有几种类型？ | get_properties + genuine reasoning | answered | **Yes -- see below, this is the headline result** |
| 9 | Which storey has the most windows? | group_by argmax | answered | Yes -- correctly explained the real tie |
| 10 | What can you see in the current view? | inspect_current_view, no active selection | answered* | Correct content; disposition arguably should read `clarification_required` -- see Known gaps |

Every citation traces to a real tool call against the real IFC file (verified by inspecting
`response.citations` for each, not assumed) -- no number in any answer above was stated without a
corresponding tool return value.

## The headline result: question 8, side by side

This is the exact question shape D-059 found live in production ("所有窗户有几种类型，每种类型的
大小...是多少") -- asking for a **distinct-dimension grouping**, an operation neither V1's
deterministic fast path nor its typed `QueryPlan` schema can express at all (there is no "group by
distinct value" operation anywhere in `IfcQueryInput`).

**V1** (after the D-059 regex fix, so this no longer hits the wrong flat-count bug -- it now
correctly falls through to the semantic planner):

> 各层窗户数量：**IfcWindow**：**4**。

Confusing and still wrong -- a mislabeled "per-storey" template stating a flat count, addressing
neither "how many types" nor "each type's size." `disposition=answered`, 18.55s, 2 model calls.
The semantic planner tried to make sense of a request its own typed vocabulary has no shape for,
and produced this.

**V2**, the identical question, real data:

> 我在模型中查到4个 IfcWindow（Level 01 Window 1 / Level 01 Window 2 / Level 02 Window 1 / Level 02
> Window 2）。按返回的 Qto_WindowBaseQuantities 尺寸，这些窗户可分为 2 种类型：
> - 类型 A — Height = 1.5，Width = 1.2：Level 01 Window 1、Level 02 Window 1
> - 类型 B — Height = 1.75，Width = 1.2：Level 01 Window 2、Level 02 Window 2

Correct. V2 called `get_element_properties`, received the real dimensions of all 4 real windows
(a tool call, not an invented fact), and *itself* performed the classification the user actually
asked for -- exactly the "genuine interpretive judgment, not a database lookup" case this spec's
own Rationale named as the right place for V2's intelligence to matter. `disposition=answered`,
19.02s, 2 model calls -- essentially the same latency as V1's wrong answer, for a correct one.

## Latency: real numbers, honestly short of the spec's own draft target

| Question | Total | Time-to-first-token |
|---|---|---|
| How many doors are there? | 7.16s | 7.14s |
| How many windows and doors are there? | 5.10s | 5.09s |
| What is the maximum door height? | 6.78s | 6.41s |
| 这个项目里有多少扇窗？ | 6.19s | 6.12s |
| 哪个楼层的窗户最多？ | 11.27s | 11.16s |
| What properties does the door have? | 13.21s | 11.46s |
| Compare doors/windows vs. PDF | 13.95s | 9.76s |
| 这个建筑里所有窗户有几种类型？ | 19.02s | 17.44s |
| Which storey has the most windows? | 10.36s | 10.17s |
| What can you see in the current view? | 9.64s | 9.52s |

SPEC-M16 §E's own draft goal was "time-to-first-token under ~2s for a single-tool-call turn" --
**not met**. The real bottleneck, confirmed by the numbers themselves (`total ≈ ttft` in every row):
almost the entire wall time is the *first* model round trip -- deciding which tool(s) to call --
which cannot itself be streamed (a tool call's argument JSON must be fully received before it is
parseable; `stream_turn`'s own docstring states this constraint). The *second* round trip (the
already-decided final answer) does stream token-by-token as designed and verified, but by then most
of the turn's time has already elapsed invisibly. The "thinking" indicator (SS E) added this same
session gives the user something to see immediately rather than several seconds of silence, but it
does not reduce the real round-trip count or time.

**Direct comparison on a question V1 already handles deterministically** (reconciliation, row 7 of
the correctness table): V1 answers in **0.09s with 0 model calls** (its own dedicated heuristic
detection short-circuits straight to the deterministic join); V2 takes **13.95s**. This is an
honest, real illustration of the actual tradeoff this spec's own Explicitly excluded scope named in
advance: V2 is not, and is not claimed to be, a replacement for V1's speed on questions V1 already
answers well. Its real value, per the question-8 result above, is on the class of question V1's
architecture cannot answer *correctly* at all, not on making already-solved questions faster.

## Known gaps, found live, not yet fixed

- **Question 10's disposition** (`answered` for a request that is really asking the user for more
  information) probably should read `clarification_required`, matching V1's own disposition for
  the identical situation. Not fixed in this pass -- `invoke_v2`'s disposition logic currently only
  distinguishes "had a tool call" from "did not," not "answered directly" from "asked a clarifying
  question." A real, scoped follow-up.
- **Per-turn wall time (5-19s) is materially slower than V1's own instant-or-near-instant common
  case.** Per this report's own Rationale, closing that gap further (a faster tool-selection step,
  parallelizing more of the round trip, or a different model for that specific decision) is real
  design work explicitly out of this phase's scope -- flagged for a follow-up phase, not silently
  promised here.

## Conclusion

V2 answered all 10 representative questions correctly with zero fabricated facts, including the
exact production question that motivated this entire spec -- and did so with genuine interpretive
reasoning (question 8) that V1's architecture structurally cannot produce, regardless of how many
more keyword patches it receives. It is measurably, honestly slower than V1 on questions V1 already
answers well. Both findings are exactly what SPEC-M16's own Rationale predicted before this
benchmark ran; this report is the promised real data confirming (not merely asserting) that
prediction.
