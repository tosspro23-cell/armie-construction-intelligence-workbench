"""Real-Postgres concurrency tests for PostgresFindingStore's row-locked
transactions (D-064 items 2/3), closing the specific gap D-064 itself
disclosed rather than claimed proven: `append_transition`'s
`expected_pending_proposal_id` check and `set_pending_proposal`'s
`expected_status` check were verified only by code review and by
InMemoryFindingStore's single-process test suite passing against the same
`FindingStore` Protocol shape -- never against a real Postgres instance
under genuine concurrent connections. `SELECT ... FOR UPDATE` semantics
under real contention are exactly the kind of thing that can look correct
reading the code and still not hold under real concurrent transactions
(lock queuing, isolation level, connection-pool interaction) -- these
tests actually run that contention, with `threading.Barrier` maximizing
real overlap, against `docker-compose.yml`'s own Postgres, not a fake.

Gated behind TEST_DATABASE_URL, matching tests/test_postgres_persistence.py's
own established pattern -- see that file's own docstring for the CI/local
opt-in story (`docker compose up postgres`, then apply migrations 0006 and
0008 for the tables these tests need).
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from app.schemas.models import (
    AgentProposal,
    Citation,
    FindingStatus,
    VerificationStatus,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a local Postgres (see docker-compose.yml) to run these",
)


@pytest.fixture(autouse=True)
def _clean_tables():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE engineering_finding_history, engineering_findings")
        conn.commit()
    yield


def _store():
    from app.persistence.postgres_store import PostgresFindingStore

    return PostgresFindingStore(TEST_DATABASE_URL)


def _seed_finding(*, pending_proposal_id: str | None, tag: str = "W02") -> str:
    """Inserts one ACTION_REQUIRED finding directly via SQL (not
    upsert_from_reconciliation, which has no way to set a specific
    pending_proposal) so each test controls the exact proposal_id under
    contention. `tag` defaults to a fixed value for single-finding tests,
    but must be distinct per iteration in a multi-trial test -- the
    partial unique index on (project_id, tag, finding_type) WHERE status
    is active (0006_engineering_findings.sql) means two active findings
    for the same tag collide.
    """
    import psycopg

    finding_id = str(uuid4())
    proposal_json = None
    if pending_proposal_id is not None:
        proposal = AgentProposal(
            proposal_id=pending_proposal_id, proposed_height_m=1.75, verdict="dimension_confirmed",
            verdict_basis="test", rationale="test",
            citations=[Citation(evidence_id="e1", source_type="ifc", label="test", locator={}, project_id="demo", source_set_id="demo-v2", source_file="armie_demo.ifc")],
            verification=VerificationStatus(status="verified", reason="test"), trace_id="t1",
        )
        proposal_json = json.dumps(proposal.model_dump(mode="json"))
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO engineering_findings
                (finding_id, project_id, source_set_id, trace_id, tag, finding_type, severity,
                 status, detail, ifc_width_m, ifc_height_m, pdf_width_m, pdf_height_m, evidence_refs, pending_proposal)
            VALUES (%s, 'demo', 'demo-v2', 't1', %s, 'dimension_mismatch', 'medium',
                    'action_required', 'test', 1.2, 1.75, 1.2, 1.7, '[]'::jsonb, %s)
            """,
            (finding_id, tag, proposal_json),
        )
        conn.commit()
    return finding_id


# --- D-064 item 2: approval version binding under real concurrency ------------------

def test_row_lock_serializes_concurrent_approvals_and_exactly_one_succeeds():
    """The concurrency claim D-064 made but never ran against a real
    database: N real, concurrent Postgres connections all try to approve
    the *same* live proposal_id at once. Without `SELECT ... FOR UPDATE`
    actually taken inside the same transaction as the write, a
    read-then-write race could let more than one succeed (each reading
    the same "still valid" snapshot before any of them commits). Ten real
    threads, released simultaneously via a barrier to maximize genuine
    overlap, against a real connection pool (max_size=5, so at least
    several are genuinely concurrent at the Postgres level, not just
    queued one at a time in application code).
    """
    from app.persistence.finding_store import FindingVersionConflict

    finding_id = _seed_finding(pending_proposal_id="proposal-a")
    store = _store()
    concurrency = 10
    barrier = threading.Barrier(concurrency)

    def _attempt(worker_id: int) -> str:
        barrier.wait()
        try:
            store.append_transition(
                finding_id, to_status=FindingStatus.RESOLVED, actor_session_id=f"reviewer-{worker_id}", note=None,
                updates={"pending_proposal": None}, proposal_snapshot=None, expected_pending_proposal_id="proposal-a",
            )
            return "ok"
        except FindingVersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(_attempt, range(concurrency)))

    assert results.count("ok") == 1, f"expected exactly one real approval to win the race, got: {results}"
    assert results.count("conflict") == concurrency - 1

    final = store.get(finding_id)
    assert final.status == FindingStatus.RESOLVED
    assert final.pending_proposal is None
    # Exactly one new history row from this race (plus none pre-existing --
    # this finding was inserted directly via SQL, not through a real
    # transition) -- proves the lock didn't let a second writer sneak in a
    # duplicate/corrupted history entry for the same event.
    assert len(final.history) == 1


def test_row_lock_rejects_a_stale_proposal_id_even_under_concurrent_load():
    """The other half of the same claim: a *stale* proposal_id must lose
    to real contention exactly as reliably as it does single-threaded --
    run alongside several concurrent attempts using the *current* id, to
    confirm the stale one's rejection isn't an artifact of it happening
    to run alone.
    """
    from app.persistence.finding_store import FindingVersionConflict

    finding_id = _seed_finding(pending_proposal_id="proposal-current")
    store = _store()
    concurrency = 6
    barrier = threading.Barrier(concurrency + 1)

    def _stale_attempt() -> str:
        barrier.wait()
        try:
            store.append_transition(
                finding_id, to_status=FindingStatus.RESOLVED, actor_session_id="stale-reviewer", note=None,
                updates={"pending_proposal": None}, proposal_snapshot=None, expected_pending_proposal_id="proposal-stale",
            )
            return "ok"
        except FindingVersionConflict:
            return "conflict"

    def _current_attempt(worker_id: int) -> str:
        barrier.wait()
        try:
            store.append_transition(
                finding_id, to_status=FindingStatus.RESOLVED, actor_session_id=f"reviewer-{worker_id}", note=None,
                updates={"pending_proposal": None}, proposal_snapshot=None, expected_pending_proposal_id="proposal-current",
            )
            return "ok"
        except FindingVersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=concurrency + 1) as pool:
        stale_future = pool.submit(_stale_attempt)
        current_futures = [pool.submit(_current_attempt, i) for i in range(concurrency)]
        stale_result = stale_future.result()
        current_results = [f.result() for f in current_futures]

    assert stale_result == "conflict"  # the stale id never wins, no matter how much real contention surrounds it
    assert current_results.count("ok") == 1
    assert current_results.count("conflict") == concurrency - 1


# --- D-064 item 3: late-investigation guard under real concurrency ------------------
#
# `updates={"pending_proposal": None}` on a plain "resolve" is what
# main.py's real `transition_finding` endpoint supplies (found live,
# independent-review finding, 2026-09-19, reproduced with *zero*
# concurrency: it didn't used to -- see finding_workflow.py's
# ACTIONS_THAT_CLEAR_PENDING_PROPOSAL docstring for the full story).
# Clearing pending_proposal is a main.py-level business decision, not
# something `append_transition` enforces on its own for every caller
# regardless of what `updates` it's given -- these tests exercise the
# store's own atomicity guarantee (does the row lock correctly serialize
# two real concurrent writers) on the assumption the caller supplies the
# correct updates, matching how main.py actually calls it; see
# test_engineering_findings.py for the separate, non-concurrency
# assertion that main.py's real endpoint actually supplies them.


def _resolve_with_cleared_proposal(store, finding_id: str) -> None:
    store.append_transition(finding_id, to_status=FindingStatus.RESOLVED, actor_session_id="human", note=None, updates={"pending_proposal": None})


def _late_investigation_proposal(tag: str) -> "AgentProposal":
    return AgentProposal(
        proposal_id=f"late-{tag}", proposed_height_m=1.75, verdict="dimension_confirmed",
        verdict_basis="late investigation result", rationale="late",
        verification=VerificationStatus(status="verified", reason="test"), trace_id="t1",
    )


def test_late_investigation_finishing_before_a_resolve_is_still_cleared_once_resolved():
    """Deterministic ordering (no threading): the investigation's write
    lands first, while the finding is still ACTION_REQUIRED, then a human
    resolves afterward. The investigation's own proposal is legitimately
    persisted in the interim, but the resolve -- happening after it --
    must still leave the finding with no proposal once done.
    """
    finding_id = _seed_finding(pending_proposal_id=None, tag="ordering-investigation-first")
    store = _store()

    store.set_pending_proposal(finding_id, _late_investigation_proposal("a"), expected_status=FindingStatus.ACTION_REQUIRED)
    assert store.get(finding_id).pending_proposal is not None  # legitimately persisted at this point

    _resolve_with_cleared_proposal(store, finding_id)

    final = store.get(finding_id)
    assert final.status == FindingStatus.RESOLVED
    assert final.pending_proposal is None


def test_resolve_finishing_before_a_late_investigation_rejects_the_late_write():
    """The other deterministic ordering: a human resolves first, then a
    slow investigation's own persistence step runs against an
    already-resolved finding -- `expected_status` must reject it.
    """
    finding_id = _seed_finding(pending_proposal_id=None, tag="ordering-resolve-first")
    store = _store()

    _resolve_with_cleared_proposal(store, finding_id)
    assert store.get(finding_id).status == FindingStatus.RESOLVED

    result = store.set_pending_proposal(finding_id, _late_investigation_proposal("b"), expected_status=FindingStatus.ACTION_REQUIRED)

    assert result is None  # the write was skipped, not silently applied
    final = store.get(finding_id)
    assert final.status == FindingStatus.RESOLVED
    assert final.pending_proposal is None


def test_row_lock_never_lets_a_late_investigation_resurrect_a_proposal_under_real_concurrency():
    """Both orderings above are individually correct by construction --
    this is the actual concurrency claim: released together via a real
    barrier so genuine overlap is likely (not guaranteed which side wins
    on any given run, which is exactly the point), the safety invariant
    must hold regardless of whichever real Postgres transaction the row
    lock lets go first: a RESOLVED finding never ends up with a
    resurrected `pending_proposal`. Run across 20 real trials -- the
    invariant, not a specific ordering, is what's asserted each time,
    since which side wins a given race is an implementation-scheduling
    detail, not something this test should depend on.
    """
    for trial in range(20):
        finding_id = _seed_finding(pending_proposal_id=None, tag=f"concurrent-{trial}")
        store = _store()
        proposal = _late_investigation_proposal(str(trial))
        barrier = threading.Barrier(2)

        def _resolve() -> None:
            barrier.wait()
            _resolve_with_cleared_proposal(store, finding_id)

        def _late_investigation() -> None:
            barrier.wait()
            store.set_pending_proposal(finding_id, proposal, expected_status=FindingStatus.ACTION_REQUIRED)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), [_resolve, _late_investigation]))

        final = store.get(finding_id)
        if final.status == FindingStatus.RESOLVED:
            assert final.pending_proposal is None, f"trial {trial}: RESOLVED finding has a resurrected proposal"
        else:
            assert final.status == FindingStatus.ACTION_REQUIRED
            assert final.pending_proposal is not None and final.pending_proposal.proposal_id == proposal.proposal_id
