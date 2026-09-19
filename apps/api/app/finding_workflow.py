from __future__ import annotations

from app.schemas.models import FindingStatus


class IllegalFindingTransition(ValueError):
    """Raised for an action that is not legal from a finding's current
    status -- apps/api/app/main.py turns this into a 409, never a silent
    no-op or an unhandled 500.
    """


# SPEC-M11 §5: the single source of truth for which human-triggered
# action is legal from which status. Both the API route and
# tests/test_engineering_findings.py's table-driven legal/illegal coverage
# read this same table, so a check added to one is never accidentally
# missing from the other. Re-verify is deliberately not here -- it is not
# a client-chosen transition (the destination is decided by re-reading the
# real sources, see AgentService.reverify_reconciliation_tag), so it has
# its own dedicated endpoint/function below instead of an entry in this
# table.
#
# SPEC-M17 §4C: `approve_proposal`/`reject_proposal` added here exactly
# like every other action, status-wise -- both are only legal from
# ACTION_REQUIRED. Neither one's *second* precondition (a pending
# proposal must actually exist) lives in this table, deliberately: that
# check is not a function of `current_status` alone the way every other
# entry here is, so main.py checks it as an explicit extra guard right
# after this table's own check passes, rather than distorting
# `validate_transition`'s single-purpose signature to carry it.
_TRANSITIONS: dict[str, dict[FindingStatus, FindingStatus]] = {
    "acknowledge": {FindingStatus.OPEN: FindingStatus.ACKNOWLEDGED},
    "start_action": {FindingStatus.ACKNOWLEDGED: FindingStatus.ACTION_REQUIRED},
    "waive": {FindingStatus.ACKNOWLEDGED: FindingStatus.WAIVED},
    "mark_false_positive": {FindingStatus.ACKNOWLEDGED: FindingStatus.FALSE_POSITIVE},
    "resolve": {FindingStatus.ACTION_REQUIRED: FindingStatus.RESOLVED},
    "approve_proposal": {FindingStatus.ACTION_REQUIRED: FindingStatus.RESOLVED},
    "reject_proposal": {FindingStatus.ACTION_REQUIRED: FindingStatus.ACTION_REQUIRED},
}

# SPEC-M17 §4C: actions legal only when the finding also has a live
# `pending_proposal` -- checked by main.py as an extra guard after
# `validate_transition` itself passes (see the comment on `_TRANSITIONS`
# above for why this doesn't live inside that table/function).
PROPOSAL_REQUIRED_ACTIONS = frozenset({"approve_proposal", "reject_proposal"})

# Independent-review finding, 2026-09-19, found live against a real
# Postgres instance under D-064's own new concurrency tests (not just a
# race -- reproduced with zero concurrency, plain sequential calls): a
# finding investigated (pending_proposal set), then resolved *manually*
# via plain "resolve" (bypassing the agent proposal entirely, exactly as
# SPEC-M11 always allowed), kept its now-stale pending_proposal forever --
# "resolve" was never in `PROPOSAL_REQUIRED_ACTIONS`, so main.py's
# `updates={"pending_proposal": None} if action in PROPOSAL_REQUIRED_ACTIONS
# else None` never cleared it. A stale proposal on an already-resolved
# finding still renders its Approve/Reject buttons in the UI (that
# rendering is gated on `pending_proposal` alone, not status), which then
# 409 if clicked -- confusing, not silently harmful, but wrong. Broader
# than a race: this widens the actions that clear `pending_proposal` to
# include plain `resolve` too, distinct from `PROPOSAL_REQUIRED_ACTIONS`
# (resolve needs no `proposal_id` confirmation -- it clears *whatever*
# proposal exists, if any, as a simple consequence of the finding no
# longer being actionable, not a decision about a specific proposal).
ACTIONS_THAT_CLEAR_PENDING_PROPOSAL = PROPOSAL_REQUIRED_ACTIONS | frozenset({"resolve"})

# SPEC-M17, amended 2026-09-19 (owner decision, live-testing session):
# originally dimension_mismatch only. Live verification against the real
# Azure deployment confirmed the chat-embedded investigation architecture
# genuinely demonstrates multi-step, multi-tool reasoning (not a scripted
# workflow) for a dimension_mismatch finding; the owner then asked to widen
# coverage to the other two SPEC-M11 finding types for fuller testing.
# `AgentService.build_finding_proposal`'s numeric extraction already
# degrades correctly for these (one side of ifc/pdf is always `None` for
# these two types -- `_extract_proposed_dimension`'s existing `expected is
# None -> no match` handling means a genuine finding's own real-side value
# still gets proposed when the agent confirms it, never a fabricated
# guess for the missing side).
INVESTIGABLE_FINDING_TYPES = frozenset({"dimension_mismatch", "missing_in_pdf", "missing_in_ifc"})


def validate_transition(current_status: FindingStatus, action: str) -> FindingStatus:
    """Returns the target status for `action` from `current_status`, or
    raises `IllegalFindingTransition` -- never returns a status the state
    machine doesn't actually allow from here.
    """
    table = _TRANSITIONS.get(action)
    if table is None:
        raise IllegalFindingTransition(f"Unknown action '{action}'.")
    target = table.get(current_status)
    if target is None:
        raise IllegalFindingTransition(f"Cannot '{action}' a finding in status '{current_status.value}'.")
    return target


def resolve_reverify_outcome(current_status: FindingStatus, *, now_matches: bool) -> FindingStatus:
    """Re-verify is legal only from RESOLVED (a human has claimed the
    underlying source was fixed); its destination is decided by the fresh
    re-read, not by the caller.
    """
    if current_status != FindingStatus.RESOLVED:
        raise IllegalFindingTransition(f"Cannot re-verify a finding in status '{current_status.value}'; only RESOLVED findings can be re-verified.")
    return FindingStatus.VERIFIED_CLOSED if now_matches else FindingStatus.ACTION_REQUIRED
