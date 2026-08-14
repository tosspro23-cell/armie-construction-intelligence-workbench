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


if __name__ == "__main__":
    make_ifc()
    make_pdf()
