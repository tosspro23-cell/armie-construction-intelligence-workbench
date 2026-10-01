"""Owner-reported, 2026-09-30: while cross-checking a door/window
dimension mismatch (Findings tab, IFC vs. PDF schedule), the owner needed
to see the IFC side's own real width/height directly in the 3D viewer --
clicking an element there, or jumping to it from an Evidence citation,
showed identity fields only (GlobalId/ExpressID/Tag), never the
measurement actually being compared. `_reconciliation_ifc_items`
(`apps/api/app/agent/graph.py`) already reads this same value for the
Findings tab's own comparison; this closes the same gap for
`viewer_elements`/`mesh_elements` (the two IFC repository methods that
back the 3D viewer's own selectable elements) and for citation evidence
locators (`IfcRepository._make_evidence`), reusing the shared
`IfcRepository.door_window_dimensions_m` extraction, not a second,
independent implementation.

Deliberately scoped to IfcDoor/IfcWindow only, matching OD-15's own
narrow reconciliation-comparison boundary -- a wall's own Qto `Width`
quantity means its thickness, not a comparable opening size, so this is
`None` for every other entity type by design, not an oversight.

Reproduced against the real demo fixture's own known element (W01 =
global_id 1ctyjgDIX8IAhrrKTlC1w0, width=1.2m/height=1.5m -- the same real
element/values `test_reconciliation.py` already established), not a
synthetic value.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DEMO_IFC = ROOT / "demo_data" / "armie_demo.ifc"
W01_GLOBAL_ID = "1ctyjgDIX8IAhrrKTlC1w0"  # Level 01 Window 1, Tag "W01", 1.2 x 1.5 m


def test_viewer_elements_now_carries_a_windows_own_real_dimensions():
    repository = IfcRepository(DEMO_IFC)

    elements = repository.viewer_elements

    w01 = next(item for item in elements if item["global_id"] == W01_GLOBAL_ID)
    assert w01["width_m"] == 1.2  # previously absent from this dict entirely
    assert w01["height_m"] == 1.5


def test_mesh_elements_now_carries_a_windows_own_real_dimensions():
    repository = IfcRepository(DEMO_IFC)

    elements = repository.mesh_elements

    w01 = next(item for item in elements if item["global_id"] == W01_GLOBAL_ID)
    assert w01["width_m"] == 1.2  # previously absent from this dict entirely
    assert w01["height_m"] == 1.5


def test_viewer_elements_leaves_dimensions_null_for_a_non_door_window_type():
    """Deliberate, not a gap: a wall's own Qto `Width` quantity means its
    thickness, not a comparable door/window opening size -- showing it in
    the same UI slot would be actively misleading, not merely incomplete.
    """
    repository = IfcRepository(DEMO_IFC)

    elements = repository.viewer_elements

    wall = next(item for item in elements if item["entity_type"] == "IfcWall")
    assert wall["width_m"] is None
    assert wall["height_m"] is None


def test_reconciliation_still_reads_the_same_real_dimensions_after_the_shared_refactor():
    """`AgentService._reconciliation_ifc_items` was refactored to call the
    same `IfcRepository.door_window_dimensions_m` this fix adds, instead
    of keeping its own independent copy of the Qto/OverallWidth-
    OverallHeight lookup -- confirms that refactor changed no behavior,
    against the same real element the tests above use.
    """
    import asyncio

    from app.agent.graph import AgentService
    from app.config import Settings
    from app.services import ServiceContainer

    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_files=["armie_demo_schedule.pdf"])
    settings.ensure_runtime_directories()
    container = ServiceContainer(settings)
    service = AgentService(container)
    resources = asyncio.run(container.get_project("demo"))

    items = service._reconciliation_ifc_items(resources)

    assert items["W01"]["width_m"] == 1.2
    assert items["W01"]["height_m"] == 1.5
