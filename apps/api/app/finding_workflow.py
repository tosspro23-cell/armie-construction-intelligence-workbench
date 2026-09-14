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
_TRANSITIONS: dict[str, dict[FindingStatus, FindingStatus]] = {
    "acknowledge": {FindingStatus.OPEN: FindingStatus.ACKNOWLEDGED},
    "start_action": {FindingStatus.ACKNOWLEDGED: FindingStatus.ACTION_REQUIRED},
    "waive": {FindingStatus.ACKNOWLEDGED: FindingStatus.WAIVED},
    "mark_false_positive": {FindingStatus.ACKNOWLEDGED: FindingStatus.FALSE_POSITIVE},
    "resolve": {FindingStatus.ACTION_REQUIRED: FindingStatus.RESOLVED},
}


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
