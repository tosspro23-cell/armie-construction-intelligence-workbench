# M1 mutation protocol

**Status:** manual only. Mutation verification is deliberately **not** automated and must
never run in CI, become a CI job, or add a dependency to the normal pipeline (SPEC-M1
§4.8/23). Its only purpose is to demonstrate, once, that F1-F12 actually detect the failure
they claim to detect -- i.e. that removing the guard the test targets makes that specific test
fail, and that the failure output names the real defect rather than an unrelated crash.

## Method

For each targeted eval:

1. Identify the production guard the eval's assertion depends on (a specific function call,
   condition, or check in `apps/api/app/`).
2. Temporarily edit that one guard so it no longer does its job -- weaken or remove the check,
   never change the eval's test file.
3. Run only that eval's test function:
   `PYTHONPATH=apps/api python3 -m pytest -q tests/test_failure_path_evals.py::<test_name>`
4. Record the failure output verbatim.
5. Revert the mutation immediately (`git checkout -- <file>` or a manual undo) and re-run the
   full suite to confirm it is green again before moving to the next mutation.

No mutation is ever committed. No mutation touches a test file. Each mutation is reverted
before the next one starts, so mutations are never combined.

## Evidence format

For each targeted eval, the PR body's "Mutation evidence" section records:

- the eval ID and the one-line guard that was disabled;
- the file/line of the mutation (as a diff snippet);
- the exact `pytest` command run;
- the observed failure output (assertion message), verbatim;
- confirmation the mutation was reverted and the full suite is green again.

## Selecting targets

SPEC-M1 §9/6 requires manual mutation evidence for at least three of F2, F5, F8, F9, F10, F11.
This run targets F8, F9, F10, and F11 (four, to exceed the minimum) because each has a single,
clearly identifiable guard whose removal should flip the test's assertion in an unambiguous,
independently-checkable way:

| Eval | Guard mutated | What the mutation does |
|---|---|---|
| F8 | `capability_gate` in `apps/api/app/agent/router.py` | Always allow, instead of rejecting an unsupported plan |
| F9 | the `cross_source_join_requested` check in `AgentService._resolve_context` (`apps/api/app/agent/graph.py`) | Skip the early cross-source-join refusal so routing proceeds |
| F10 | the disposition guard on the conversation-context commit in `apps/api/app/main.py`'s `chat()` | Commit context for every disposition, including `cancelled` |
| F11 | the `verify_execution_consistency` call in `AgentService._execute_ifc` (`apps/api/app/agent/graph.py`) | Skip the result-shape consistency check entirely |

F11's mutation is the most safety-relevant: disabling it turns a shape-mismatched result from
`disposition=error` into `disposition=answered` -- i.e. it demonstrates the guard that stands
between a plan/tool mismatch and an unverified numeric claim being presented as verified.
