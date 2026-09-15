"""D-045: `IfcRepository._make_evidence` used to hard-cap its evidence
sample to 10 elements regardless of how many actually matched or what the
query's own `limit` already allowed -- an owner-reported real-world case
was a "how many windows" question against the real DigitalHub building
(47 real `IfcWindow` matches) showing only 10 items of evidence. Fixed to
reuse `IfcQueryInput.limit` (already validated 1-200) instead of a second,
disconnected constant.

Reproduced against the real Duplex fixture (57 real walls, well past the
old hardcoded 10) rather than a synthetic case, since the defect is
specifically about a real building's real match count.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas.models import IfcQueryInput
from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DUPLEX_IFC = ROOT / "demo_data" / "projects" / "duplex" / "Duplex_A_20110907.ifc"


def test_count_evidence_is_no_longer_hard_capped_at_ten_on_a_large_real_match_set():
    repository = IfcRepository(DUPLEX_IFC)
    result = repository.execute(IfcQueryInput(operation="count", entity_type="IfcWall"))

    assert result.value == 57
    # The old hardcoded cap silently returned exactly 10 evidence items no
    # matter how many walls matched -- this asserts the fix, not just "more
    # than 10 by coincidence."
    assert len(result.evidence) > 10


def test_evidence_sample_size_is_bounded_by_the_querys_own_limit_not_unbounded():
    repository = IfcRepository(DUPLEX_IFC)
    result = repository.execute(IfcQueryInput(operation="count", entity_type="IfcWall", limit=20))

    assert result.matched_count == 57
    # A caller-specified limit still bounds the evidence sample -- the fix
    # reuses the existing, already-validated `limit` field as the one
    # source of truth, it does not remove bounding altogether.
    assert len(result.evidence) == 20


def test_evidence_covers_every_match_when_the_match_count_is_within_the_default_limit():
    repository = IfcRepository(DUPLEX_IFC)
    # Duplex's real IfcWindow count (see D-041/D-042's own live checks) is
    # well under IfcQueryInput.limit's default of 50 -- the exact shape of
    # the owner-reported DigitalHub case (47 windows, default limit 50).
    result = repository.execute(IfcQueryInput(operation="count", entity_type="IfcWindow"))

    assert len(result.evidence) == result.value
