from __future__ import annotations

import threading
from typing import Protocol

from app.schemas.models import (
    AgentProposal,
    EngineeringFinding,
    FindingHistoryEntry,
    FindingStatus,
    utc_now,
)

# A finding in any of these statuses is still "in flight" -- a fresh
# reconciliation run detecting the same tag/finding_type updates this row
# rather than creating a duplicate. RESOLVED counts as active on purpose
# (SPEC-M11 §4B): a human's claimed fix hasn't been re-verified yet, so the
# underlying condition being detected again should still land on the same
# finding, not a second one for the same mismatch.
ACTIVE_STATUSES = frozenset({
    FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED, FindingStatus.ACTION_REQUIRED, FindingStatus.RESOLVED,
})


class FindingVersionConflict(Exception):
    """Independent-review finding, 2026-09-19 (live-testing session): raised
    when a write's stated precondition (which specific `pending_proposal`
    the caller believes they are approving/rejecting) no longer matches the
    finding's actual current state -- e.g. a second investigation replaced
    it, or another reviewer already acted on it, in the window between the
    caller loading the page and clicking a button. The write is rejected
    entirely, never partially applied and never silently approving/
    rejecting a different proposal than the one the caller actually saw.
    `apps/api/app/main.py` turns this into a 409, the same way
    `IllegalFindingTransition` already is for a state-machine violation --
    this is a different kind of conflict (identity/version, not legality)
    but the same "never silently proceed" contract.
    """


class FindingStore(Protocol):
    """Persistence for `EngineeringFinding` (SPEC-M11).

    `InMemoryFindingStore` is the local-development/test default;
    `PostgresFindingStore` (persistence/postgres_store.py) is the
    production-durable implementation. Both satisfy this exact interface
    so call sites in graph.py/main.py never need to know which one is live.
    """

    def upsert_from_reconciliation(
        self, *, project_id: str, source_set_id: str, trace_id: str, tag: str,
        finding_type: str, severity: str, detail: str,
        ifc_width_m: float | None, ifc_height_m: float | None,
        pdf_width_m: float | None, pdf_height_m: float | None,
        evidence_refs: list[str],
    ) -> EngineeringFinding:
        """Create a new OPEN finding for (project_id, tag, finding_type), or
        update the existing *active* one in place (new detail/observed
        values, `updated_at` bumped) if one already exists -- never a
        duplicate for the same still-open condition.
        """
        ...

    def get(self, finding_id: str) -> EngineeringFinding | None: ...

    def list_for_project(self, project_id: str, status: FindingStatus | None = None) -> list[EngineeringFinding]: ...

    def append_transition(
        self, finding_id: str, *, to_status: FindingStatus, actor_session_id: str | None, note: str | None,
        updates: dict | None = None, proposal_snapshot: AgentProposal | None = None,
        expected_pending_proposal_id: str | None = None,
    ) -> EngineeringFinding:
        """Append one `FindingHistoryEntry` and update `status` (plus any
        `updates`, e.g. a re-verify's fresh `detail`/observed values) on the
        finding identified by `finding_id`. Raises `KeyError` if it does
        not exist -- callers are responsible for the state-machine legality
        check (FindingService.transition, apps/api/app/services.py)
        *before* calling this; this method only records the result.

        `proposal_snapshot` (SPEC-M17 §4C, widened 2026-09-19 to `reject_
        proposal` too -- see D-063's amendment): an immutable copy of the
        exact `AgentProposal` being approved/rejected, written onto the
        created `FindingHistoryEntry` itself, independent of whatever
        `updates` does to the finding's own live `pending_proposal` field
        (typically clearing it to `None`).

        `expected_pending_proposal_id` (independent-review finding,
        2026-09-19): when given, this call is atomically checked against
        the finding's *current* `pending_proposal.proposal_id` (read fresh
        inside this same lock/transaction, not the caller's possibly-stale
        snapshot) and raises `FindingVersionConflict` on any mismatch --
        including the current proposal being `None` entirely. This is how
        `approve_proposal`/`reject_proposal` guarantee a human approves
        the *specific* proposal they actually reviewed, not whichever one
        happens to be live by the time the request reaches the store.
        """
        ...

    def set_pending_proposal(
        self, finding_id: str, proposal: AgentProposal | None, *, expected_status: FindingStatus | None = None,
    ) -> EngineeringFinding | None:
        """SPEC-M17 §4B: set (or clear, with `None`) a finding's live
        `pending_proposal` field. Deliberately not `append_transition`: a
        `propose-resolution` call is not a state-machine transition (it
        never changes `status`) and must not create a `FindingHistoryEntry`
        -- see the finding's own field docstring. Raises `KeyError` if
        `finding_id` does not exist.

        `expected_status` (independent-review finding, 2026-09-19): when
        given, checked atomically against the finding's *current* status
        (read fresh inside this same lock/transaction). On a mismatch --
        a human resolved/waived/marked-false-positive the finding while
        this investigation was still running -- the write is skipped
        entirely and `None` is returned, rather than resurrecting a
        `pending_proposal` on a finding no longer `ACTION_REQUIRED`. This
        is a softer contract than `append_transition`'s `FindingVersionConflict`
        (an investigation completing late is routine, not a caller error),
        so the caller decides what "skipped" means for it -- see
        `main.py`'s own handling, which logs it rather than failing the
        turn that already produced a real, displayed answer.
        """
        ...


class InMemoryFindingStore:
    """The default, process-local implementation -- mirrors
    `InMemoryConversationStore`'s own shape and locking discipline exactly.
    """

    def __init__(self) -> None:
        self._findings: dict[str, EngineeringFinding] = {}
        # Store calls arrive via asyncio.to_thread -- real OS threads, not
        # just concurrent coroutines on one event loop -- so plain dict
        # mutation here is not race-safe without this, the same reasoning
        # InMemoryConversationStore's own lock documents.
        self._lock = threading.Lock()

    def upsert_from_reconciliation(
        self, *, project_id: str, source_set_id: str, trace_id: str, tag: str,
        finding_type: str, severity: str, detail: str,
        ifc_width_m: float | None, ifc_height_m: float | None,
        pdf_width_m: float | None, pdf_height_m: float | None,
        evidence_refs: list[str],
    ) -> EngineeringFinding:
        with self._lock:
            existing = next(
                (
                    item for item in self._findings.values()
                    if item.project_id == project_id and item.tag == tag
                    and item.finding_type == finding_type and item.status in ACTIVE_STATUSES
                ),
                None,
            )
            if existing is not None:
                updated = existing.model_copy(update={
                    "trace_id": trace_id, "detail": detail,
                    "ifc_width_m": ifc_width_m, "ifc_height_m": ifc_height_m,
                    "pdf_width_m": pdf_width_m, "pdf_height_m": pdf_height_m,
                    "evidence_refs": evidence_refs, "updated_at": utc_now(),
                })
                self._findings[updated.finding_id] = updated
                return updated
            finding = EngineeringFinding(
                project_id=project_id, source_set_id=source_set_id, trace_id=trace_id, tag=tag,
                finding_type=finding_type, severity=severity, status=FindingStatus.OPEN, detail=detail,
                ifc_width_m=ifc_width_m, ifc_height_m=ifc_height_m,
                pdf_width_m=pdf_width_m, pdf_height_m=pdf_height_m, evidence_refs=evidence_refs,
                history=[FindingHistoryEntry(from_status=None, to_status=FindingStatus.OPEN, actor_session_id=None, note="Detected by reconciliation.")],
            )
            self._findings[finding.finding_id] = finding
            return finding

    def get(self, finding_id: str) -> EngineeringFinding | None:
        return self._findings.get(finding_id)

    def list_for_project(self, project_id: str, status: FindingStatus | None = None) -> list[EngineeringFinding]:
        items = [item for item in self._findings.values() if item.project_id == project_id]
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def append_transition(
        self, finding_id: str, *, to_status: FindingStatus, actor_session_id: str | None, note: str | None,
        updates: dict | None = None, proposal_snapshot: AgentProposal | None = None,
        expected_pending_proposal_id: str | None = None,
    ) -> EngineeringFinding:
        with self._lock:
            existing = self._findings.get(finding_id)
            if existing is None:
                raise KeyError(finding_id)
            if expected_pending_proposal_id is not None:
                current_id = existing.pending_proposal.proposal_id if existing.pending_proposal else None
                if current_id != expected_pending_proposal_id:
                    raise FindingVersionConflict(
                        f"Finding '{finding_id}' pending_proposal has changed since it was loaded "
                        f"(expected proposal '{expected_pending_proposal_id}', current is {current_id!r}) -- "
                        "reload the finding and review its current proposal before approving/rejecting."
                    )
            entry = FindingHistoryEntry(
                from_status=existing.status, to_status=to_status, actor_session_id=actor_session_id,
                note=note, proposal_snapshot=proposal_snapshot,
            )
            updated = existing.model_copy(update={
                "status": to_status,
                "last_actor_session_id": actor_session_id,
                "updated_at": utc_now(),
                "history": [*existing.history, entry],
                **(updates or {}),
            })
            self._findings[finding_id] = updated
            return updated

    def set_pending_proposal(
        self, finding_id: str, proposal: AgentProposal | None, *, expected_status: FindingStatus | None = None,
    ) -> EngineeringFinding | None:
        with self._lock:
            existing = self._findings.get(finding_id)
            if existing is None:
                raise KeyError(finding_id)
            if expected_status is not None and existing.status != expected_status:
                return None
            updated = existing.model_copy(update={"pending_proposal": proposal, "updated_at": utc_now()})
            self._findings[finding_id] = updated
            return updated
