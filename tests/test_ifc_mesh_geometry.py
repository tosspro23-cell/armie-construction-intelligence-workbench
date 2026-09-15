"""SPEC-M12: `IfcRepository.mesh_elements`, the real triangulated-geometry
counterpart to `viewer_elements`'s deliberate bounding-box simplification.

Acceptance criteria this covers: real vertex/triangle counts against the
real Duplex fixture match an independent direct `ifcopenshell` check (the
same reproduction technique D-041's `is_external` verification already
used, not merely "does not raise"); the synthetic `demo` fixture's fallback
path returns a valid box mesh for every element with no exception; and
`is_external`/`color` agree with `viewer_elements`'s own values for the
same element, since the two cached properties must never silently drift
apart on anything but geometry representation.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DUPLEX_IFC = ROOT / "demo_data" / "projects" / "duplex" / "Duplex_A_20110907.ifc"
DEMO_IFC = ROOT / "demo_data" / "armie_demo.ifc"


def test_mesh_elements_matches_independent_ifcopenshell_geometry_check_on_real_duplex():
    import ifcopenshell
    import ifcopenshell.geom

    repository = IfcRepository(DUPLEX_IFC)
    mesh = repository.mesh_elements
    assert len(mesh) > 0

    model = ifcopenshell.open(str(DUPLEX_IFC))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    expected_by_id: dict[int, tuple[int, int]] = {}
    for entity_type in IfcRepository._SUPPORTED_TYPES:
        for element in model.by_type(entity_type)[: IfcRepository._PER_TYPE_LIMITS[entity_type]]:
            try:
                shape = ifcopenshell.geom.create_shape(settings, element)
                vertex_count = len(shape.geometry.verts) // 3
                triangle_count = len(shape.geometry.faces) // 3
            except Exception:
                continue
            expected_by_id[element.id()] = (vertex_count, triangle_count)

    checked_real_geometry = 0
    for item in mesh:
        expected = expected_by_id.get(item["express_id"])
        if expected is None:
            continue
        checked_real_geometry += 1
        assert len(item["vertices"]) // 3 == expected[0]
        assert len(item["faces"]) // 3 == expected[1]
    # Guards against a vacuously-true check (e.g. an id-matching bug that
    # made every lookup miss) -- Duplex's real doors/windows/walls must
    # actually have been compared, not silently skipped.
    assert checked_real_geometry > 50


def test_mesh_elements_faces_are_valid_triangle_indices_into_the_vertex_array():
    repository = IfcRepository(DUPLEX_IFC)
    for item in repository.mesh_elements:
        vertex_count = len(item["vertices"]) // 3
        assert len(item["faces"]) % 3 == 0
        assert all(0 <= index < vertex_count for index in item["faces"])


def test_mesh_elements_returns_a_valid_mesh_for_every_element_on_the_synthetic_demo_fixture():
    repository = IfcRepository(DEMO_IFC)
    mesh = repository.mesh_elements
    assert len(mesh) > 0
    # Some of the demo fixture's elements do carry real triangulatable
    # geometry; others don't and must resolve through the 8-vertex/
    # 12-triangle fallback box instead of raising -- this asserts every
    # element (whichever path it took) is a well-formed mesh, and that at
    # least one genuinely took the fallback path (otherwise this test
    # would not actually exercise it).
    fallback_count = 0
    for item in mesh:
        vertex_count = len(item["vertices"]) // 3
        assert len(item["vertices"]) % 3 == 0
        assert len(item["faces"]) % 3 == 0
        assert vertex_count > 0
        assert max(item["faces"]) < vertex_count
        if vertex_count == 8 and len(item["faces"]) == 36:
            fallback_count += 1
    assert fallback_count > 0


def test_mesh_elements_agrees_with_viewer_elements_on_identity_and_material_fields():
    repository = IfcRepository(DUPLEX_IFC)
    mesh_by_id = {item["express_id"]: item for item in repository.mesh_elements}
    viewer_by_id = {item["express_id"]: item for item in repository.viewer_elements}
    assert set(mesh_by_id) == set(viewer_by_id)
    for express_id, mesh_item in mesh_by_id.items():
        viewer_item = viewer_by_id[express_id]
        assert mesh_item["color"] == viewer_item["color"]
        assert mesh_item["is_external"] == viewer_item["is_external"]
        assert mesh_item["entity_type"] == viewer_item["entity_type"]
        assert mesh_item["global_id"] == viewer_item["global_id"]
