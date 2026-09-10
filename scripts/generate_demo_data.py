"""Generate small, fully synthetic public demo assets for the workbench."""

from __future__ import annotations

from pathlib import Path

import fitz
import ifcopenshell
import ifcopenshell.api
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "demo_data"


def make_ifc() -> None:
    model = ifcopenshell.api.run("project.create_file", version="IFC4")
    project = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcProject", name="ARMIE Demo Project")
    site = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcSite", name="ARMIE Demo Site")
    building = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcBuilding", name="ARMIE Demo Building")
    storeys = [
        ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="Level 01", predefined_type=None),
        ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcBuildingStorey", name="Level 02", predefined_type=None),
    ]
    for level, elevation in zip(storeys, (0.0, 3.2)):
        level.Elevation = elevation

    ifcopenshell.api.run("unit.assign_unit", model, length={"is_metric": True, "raw": "METERS"})
    model_context = ifcopenshell.api.run("context.add_context", model, context_type="Model")
    body_context = ifcopenshell.api.run(
        "context.add_context", model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model_context
    )

    for children, parent in (([site], project), ([building], site), (storeys, building)):
        ifcopenshell.api.run("aggregate.assign_object", model, products=children, relating_object=parent)
    # Simple walls provide visible context for the browser viewer.
    for level, z in zip(storeys, (0.0, 3.2)):
        walls = []
        for x1, y1, x2, y2 in ((0, 0, 12, 0), (12, 0, 12, 8), (12, 8, 0, 8), (0, 8, 0, 0)):
            wall = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcWall", name=f"{level.Name} perimeter wall")
            representation = ifcopenshell.api.run("geometry.create_2pt_wall", model, element=wall, context=body_context, p1=(x1, y1), p2=(x2, y2), elevation=z, height=3.0, thickness=0.2)
            ifcopenshell.api.run("geometry.assign_representation", model, product=wall, representation=representation)
            walls.append(wall)
        ifcopenshell.api.run("spatial.assign_container", model, products=walls, relating_structure=level)

        for index in range(2):
            door = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcDoor", name=f"{level.Name} Door {index + 1}")
            door.OverallHeight = 2.1
            door.OverallWidth = 0.9
            door_qto = ifcopenshell.api.run("pset.add_qto", model, product=door, name="Qto_DoorBaseQuantities")
            ifcopenshell.api.run("pset.edit_qto", model, qto=door_qto, properties={"Height": 2.1, "Width": 0.9})
            representation = ifcopenshell.api.run("geometry.add_door_representation", model, context=body_context, overall_height=2.1, overall_width=0.9)
            ifcopenshell.api.run("geometry.assign_representation", model, product=door, representation=representation)
            ifcopenshell.api.run("geometry.edit_object_placement", model, product=door, matrix=np.array([[1, 0, 0, 1.2 + index * 2.0], [0, 1, 0, 0.1], [0, 0, 1, z], [0, 0, 0, 1.0]]))
            ifcopenshell.api.run("spatial.assign_container", model, products=[door], relating_structure=level)

            window = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcWindow", name=f"{level.Name} Window {index + 1}")
            window.OverallHeight = 1.5 + index * 0.25
            window.OverallWidth = 1.2
            window_qto = ifcopenshell.api.run("pset.add_qto", model, product=window, name="Qto_WindowBaseQuantities")
            ifcopenshell.api.run("pset.edit_qto", model, qto=window_qto, properties={"Height": window.OverallHeight, "Width": 1.2})
            representation = ifcopenshell.api.run("geometry.add_window_representation", model, context=body_context, overall_height=window.OverallHeight, overall_width=1.2)
            ifcopenshell.api.run("geometry.assign_representation", model, product=window, representation=representation)
            ifcopenshell.api.run("geometry.edit_object_placement", model, product=window, matrix=np.array([[1, 0, 0, 3.0 + index * 3.0], [0, 1, 0, 7.9], [0, 0, 1, z + 0.8], [0, 0, 0, 1.0]]))
            ifcopenshell.api.run("spatial.assign_container", model, products=[window], relating_structure=level)

    DATA.mkdir(exist_ok=True)
    model.write(DATA / "armie_demo.ifc")


def make_pdf() -> None:
    DATA.mkdir(exist_ok=True)
    document = fitz.open()
    page = document.new_page(width=842, height=595)
    page.insert_text((42, 45), "ARMIE DEMO ENGINEERING SCHEDULE", fontsize=18, fontname="helv", color=(0.08, 0.2, 0.35))
    page.insert_text((42, 68), "Synthetic public fixture · not a real project document", fontsize=9, fontname="helv", color=(0.3, 0.3, 0.3))
    rows = [("Board", "Connected Load (kW)", "Diversity Factor"), ("DB-L1-A", "18.50", "0.75"), ("DB-L2-B", "26.00", "0.65"), ("Panel-A", "44.50", "0.70")]
    x = [48, 250, 495, 700]
    y = 125
    for row_index, row in enumerate(rows):
        top = y + row_index * 58
        page.draw_rect(fitz.Rect(x[0], top - 25, x[-1], top + 20), color=(0.4, 0.5, 0.65), fill=(0.92, 0.95, 0.98) if row_index == 0 else (1, 1, 1), width=0.8)
        for col, value in enumerate(row):
            page.insert_text((x[col] + 8, top), value, fontsize=11 if row_index else 10, fontname="helv", color=(0.05, 0.1, 0.2))
    page.insert_text((48, 390), "Notes", fontsize=12, fontname="helv", color=(0.08, 0.2, 0.35))
    page.insert_textbox(fitz.Rect(48, 405, 760, 475), "All identifiers and values in this drawing are synthetic demo data generated for ARMIE AI Labs.\nUse the workbench to retrieve fields with page and region evidence.", fontsize=11, fontname="helv", color=(0.15, 0.15, 0.15))
    document.save(DATA / "armie_demo_schedule.pdf")


# SPEC-M6: a real, moderately-sized multi-document corpus, kept entirely
# separate from armie_demo_schedule.pdf/armie_demo.ifc (make_pdf/make_ifc
# above) -- neither of those is touched, since SPEC-M2's door/window Tag
# and reconciliation fixture work already depends on the original schedule
# staying exactly as it is (see this spec's stop conditions). Written to
# demo_data/corpus/, not demo_data/ directly, so the two are never
# confused at a glance.
CORPUS_DIR = DATA / "corpus"

# Reuses armie_demo_schedule.pdf's exact table geometry (same x bands, row
# pitch, fonts) so DocumentAnalyzer.native_lookup's characterization
# (D-009 -- clean, left-aligned, this fixture's specific layout) holds for
# every new tabular document without re-deriving it per document.
_TABLE_X = [48, 250, 495, 700]


def _make_table_document(path: Path, title: str, subtitle: str, headers: tuple[str, str, str], rows: list[tuple[str, str, str]]) -> None:
    document = fitz.open()
    page = document.new_page(width=842, height=595)
    page.insert_text((42, 45), title, fontsize=16, fontname="helv", color=(0.08, 0.2, 0.35))
    page.insert_text((42, 66), subtitle, fontsize=9, fontname="helv", color=(0.3, 0.3, 0.3))
    all_rows = [headers, *rows]
    y = 120
    for row_index, row in enumerate(all_rows):
        top = y + row_index * 42
        page.draw_rect(fitz.Rect(_TABLE_X[0], top - 22, _TABLE_X[-1], top + 14), color=(0.4, 0.5, 0.65), fill=(0.92, 0.95, 0.98) if row_index == 0 else (1, 1, 1), width=0.8)
        for col, value in enumerate(row):
            page.insert_text((_TABLE_X[col] + 8, top), value, fontsize=10.5 if row_index else 10, fontname="helv", color=(0.05, 0.1, 0.2))
    document.save(path)


def _make_narrative_document(path: Path, title: str, subtitle: str, paragraphs: list[str]) -> None:
    """A single flowing textbox, not a table -- DocumentAnalyzer._read_table
    is expected to find no >=2-column header row here (SPEC-M6 §D: distractor
    documents are not required to satisfy D-009's tabular characterization),
    so native_lookup gracefully misses on these rather than crashing or
    fabricating a bogus table.
    """
    document = fitz.open()
    page = document.new_page(width=842, height=595)
    page.insert_text((42, 45), title, fontsize=15, fontname="helv", color=(0.08, 0.2, 0.35))
    page.insert_text((42, 66), subtitle, fontsize=9, fontname="helv", color=(0.3, 0.3, 0.3))
    page.insert_textbox(fitz.Rect(42, 95, 800, 560), "\n\n".join(paragraphs), fontsize=11, fontname="helv", color=(0.15, 0.15, 0.15))
    document.save(path)


def make_document_corpus() -> None:
    """SPEC-M6's ~15-20 document, 3-4 type corpus.

    Explicitly a mechanism-demonstration scale (see PROJECT_STATE.md's
    Phase 3 scoping note and SPEC-M6's Rationale) built to evidence two
    failure modes of the naive deterministic multi-document baseline
    (app/agent/graph.py's _execute_pdf_multi_document), not an
    enterprise-scale claim:

    - Precision failure: "Panel-A" is a distinct, real panel in both
      schedule_l2_east.pdf and schedule_l2_west.pdf (two different wings
      independently using the same generic panel label -- plausible, not
      contrived) with a "Connected Load" column in each, so a question
      naming only "Panel-A" is genuinely answerable from either.
    - Recall failure: rfi_log_047.pdf describes "Panel-E" being sized for
      a "connected capacity of 15.75 kW" in prose -- deliberately not the
      tables' "Connected Load (kW)" column vocabulary -- and explicitly
      states this panel is not yet in any issued schedule. No table in
      this corpus contains "Panel-E" at all.

    None of these files are in Settings.pdf_files' default (still just
    armie_demo_schedule.pdf) -- they exist for tests/tools/services.py
    consumers that opt in by explicitly listing them.
    """
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    headers = ("Board", "Connected Load (kW)", "Diversity Factor")

    _make_table_document(
        CORPUS_DIR / "schedule_l2_east.pdf",
        "LEVEL 02 EAST WING DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-A", "32.00", "0.72"), ("Panel-C", "19.50", "0.68")],
    )
    _make_table_document(
        CORPUS_DIR / "schedule_l2_west.pdf",
        "LEVEL 02 WEST WING DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-A", "28.00", "0.70"), ("Panel-D", "21.00", "0.66")],
    )
    _make_table_document(
        CORPUS_DIR / "schedule_l3_east.pdf",
        "LEVEL 03 EAST WING DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-B", "34.00", "0.71"), ("Panel-F", "17.25", "0.60")],
    )
    _make_table_document(
        CORPUS_DIR / "schedule_l3_west.pdf",
        "LEVEL 03 WEST WING DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-B", "30.50", "0.69"), ("Panel-G", "22.75", "0.64")],
    )
    _make_table_document(
        CORPUS_DIR / "schedule_mezzanine.pdf",
        "MEZZANINE LEVEL DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-H", "12.00", "0.55"), ("Panel-I", "14.30", "0.58")],
    )
    _make_table_document(
        CORPUS_DIR / "schedule_roof_plant.pdf",
        "ROOF PLANT ROOM DISTRIBUTION SCHEDULE", "Synthetic public fixture · not a real project document",
        headers, [("Panel-J", "40.00", "0.80"), ("Panel-K", "36.20", "0.77")],
    )

    spec_headers = ("Item", "Fire Rating", "Frame Material")
    _make_table_document(
        CORPUS_DIR / "door_spec_sheet_package_a.pdf",
        "DOOR HARDWARE PACKAGE A -- SPECIFICATION SHEET", "Synthetic public fixture · not a real project document",
        spec_headers, [("D-101", "90 min", "Hollow Metal"), ("D-102", "60 min", "Wood")],
    )
    _make_table_document(
        CORPUS_DIR / "door_spec_sheet_package_b.pdf",
        "DOOR HARDWARE PACKAGE B -- SPECIFICATION SHEET", "Synthetic public fixture · not a real project document",
        spec_headers, [("D-201", "90 min", "Aluminum"), ("D-202", "20 min", "Wood")],
    )
    window_headers = ("Item", "Glazing Type", "Frame Material")
    _make_table_document(
        CORPUS_DIR / "window_spec_sheet_package_a.pdf",
        "WINDOW GLAZING PACKAGE A -- SPECIFICATION SHEET", "Synthetic public fixture · not a real project document",
        window_headers, [("W-101", "Double IGU", "Aluminum"), ("W-102", "Triple IGU", "Vinyl")],
    )
    _make_table_document(
        CORPUS_DIR / "window_spec_sheet_package_b.pdf",
        "WINDOW GLAZING PACKAGE B -- SPECIFICATION SHEET", "Synthetic public fixture · not a real project document",
        window_headers, [("W-201", "Double IGU", "Fiberglass"), ("W-202", "Laminated", "Aluminum")],
    )

    _make_narrative_document(
        CORPUS_DIR / "rfi_log_047.pdf",
        "RFI-047 -- ELECTRICAL: ADDITIONAL MEZZANINE DISTRIBUTION PANEL", "Synthetic public fixture · not a real project document",
        [
            "Question: The mechanical contractor has requested a dedicated distribution panel to serve new "
            "equipment on the Level 02 mechanical mezzanine. This panel, referred to on site as Panel-E, "
            "was not part of the originally issued electrical package.",
            "Response: The design team has sized Panel-E for a connected capacity of 15.75 kW, based on the "
            "mechanical contractor's submitted equipment list. This panel does not yet appear in the issued "
            "electrical schedule; the schedule will be reissued at the next drawing revision. Until then, this "
            "RFI response is the current record of Panel-E's capacity.",
        ],
    )
    _make_narrative_document(
        CORPUS_DIR / "rfi_log_052.pdf",
        "RFI-052 -- STRUCTURAL: BEAM POCKET CLEARANCE AT GRID C-4", "Synthetic public fixture · not a real project document",
        [
            "Question: The steel beam pocket detailed at Grid C-4 conflicts with an electrical conduit run "
            "shown on the coordination drawings. Please confirm whether the beam pocket depth can be reduced.",
            "Response: The structural engineer confirmed the beam pocket depth may be reduced by 25mm without "
            "affecting the bearing capacity. The electrical subcontractor should re-route the conduit run "
            "through the adjusted pocket at the next site visit.",
        ],
    )
    _make_narrative_document(
        CORPUS_DIR / "rfi_log_058.pdf",
        "RFI-058 -- MECHANICAL: DUCT ROUTING CONFLICT ABOVE CORRIDOR", "Synthetic public fixture · not a real project document",
        [
            "Question: The supply air duct routed above the Level 01 main corridor conflicts with an existing "
            "electrical circuit breaker panel enclosure shown on the reflected ceiling plan.",
            "Response: The mechanical contractor will offset the duct run by 150mm to clear the panel "
            "enclosure. No change to the electrical schedule or any panel's connected load is required.",
        ],
    )
    _make_narrative_document(
        CORPUS_DIR / "rfi_log_061.pdf",
        "RFI-061 -- FINISHES: FIRE RATING CLARIFICATION FOR CORRIDOR DOORS", "Synthetic public fixture · not a real project document",
        [
            "Question: The finishes schedule does not clearly state the required fire rating for the doors "
            "serving the Level 01 egress corridor. Please clarify against the issued door hardware packages.",
            "Response: All egress corridor doors on Level 01 must match the 90-minute rating already specified "
            "for Package A hollow-metal doors. Refer to the door hardware specification sheets for the "
            "governing values; this RFI does not change any previously issued rating.",
        ],
    )

    _make_narrative_document(
        CORPUS_DIR / "meeting_minutes_2026_02_10.pdf",
        "PROJECT PROGRESS MEETING MINUTES -- 2026-02-10", "Synthetic public fixture · not a real project document",
        [
            "Attendees: General Contractor, Architect, MEP Engineer, Owner's Representative.",
            "Electrical: The electrical subcontractor reported that panel schedules and load calculations for "
            "Levels 02 and 03 are progressing on schedule, with issue expected at the next design milestone.",
            "Action items: Architect to confirm door hardware package assignments by the next meeting.",
        ],
    )
    _make_narrative_document(
        CORPUS_DIR / "meeting_minutes_2026_02_24.pdf",
        "PROJECT PROGRESS MEETING MINUTES -- 2026-02-24", "Synthetic public fixture · not a real project document",
        [
            "Attendees: General Contractor, Architect, MEP Engineer, Owner's Representative.",
            "Electrical: Panel schedule revisions for the east and west wings are in progress following "
            "coordination comments from the mechanical trade. No open RFIs were discussed.",
            "Action items: MEP Engineer to circulate updated schedules once issued.",
        ],
    )
    _make_narrative_document(
        CORPUS_DIR / "meeting_minutes_2026_03_10.pdf",
        "PROJECT PROGRESS MEETING MINUTES -- 2026-03-10", "Synthetic public fixture · not a real project document",
        [
            "Attendees: General Contractor, Architect, MEP Engineer, Owner's Representative.",
            "Electrical: RFI-047's response was issued and the new mezzanine distribution panel is being "
            "coordinated directly with the mechanical contractor. No further action required at this time.",
            "Action items: None outstanding for electrical scope.",
        ],
    )


if __name__ == "__main__":
    make_ifc()
    make_pdf()
    make_document_corpus()
