"""Owner-reported, 2026-09-21: while testing D-070/D-072's finding-
investigation fixes, the owner noticed the 3D viewer's own selection
details panel never showed an element's Tag/Mark -- only GlobalId/
ExpressID (the IFC model's own internal identifiers), which have no
equivalent on a PDF schedule. A human visually cross-checking the model
against a PDF schedule (exactly the kind of manual verification a
`dimension_mismatch`/`missing_in_pdf`/`missing_in_ifc` finding asks a
reviewer to do) had no way to see which schedule row a clicked element
corresponds to. `get_element_properties`/citation locators already surface
this same `element.Tag` attribute (`test_ifc_element_tag_exposure.py`'s own
fix); this closes the same gap for `viewer_elements`/`mesh_elements`, the
two IFC repository methods that back the 3D viewer's own selectable
elements (`GET /api/v1/project/viewer-elements`/`viewer-mesh`).

Reproduced against the real demo fixture's own known tag (W01 = global_id
1ctyjgDIX8IAhrrKTlC1w0, Tag "W01" -- the same real element
`test_ifc_element_tag_exposure.py` already uses), not a synthetic value.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DEMO_IFC = ROOT / "demo_data" / "armie_demo.ifc"
W01_GLOBAL_ID = "1ctyjgDIX8IAhrrKTlC1w0"  # Level 01 Window 1, Tag "W01"


def test_viewer_elements_now_carries_each_elements_own_tag():
    repository = IfcRepository(DEMO_IFC)

    elements = repository.viewer_elements

    w01 = next(item for item in elements if item["global_id"] == W01_GLOBAL_ID)
    assert w01["tag"] == "W01"  # previously absent from this dict entirely


def test_mesh_elements_now_carries_each_elements_own_tag():
    repository = IfcRepository(DEMO_IFC)

    elements = repository.mesh_elements

    w01 = next(item for item in elements if item["global_id"] == W01_GLOBAL_ID)
    assert w01["tag"] == "W01"  # previously absent from this dict entirely
