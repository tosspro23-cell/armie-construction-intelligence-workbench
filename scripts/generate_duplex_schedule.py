"""One-off generator for the Duplex Apartment door/window schedule PDF.

Not part of scripts/generate_demo_data.py (that script's own header notes
it no longer reproduces the committed synthetic fixtures byte-for-byte;
this is a separate, real-building fixture with its own provenance, per
D-035/D-036/D-037). Reads the real, unmodified Duplex_A_20110907.ifc
directly via ifcopenshell (falling back to OverallWidth/OverallHeight when
no IfcElementQuantity set is attached -- this file has none at all, the
same D-037 gap found on the WBDG Office candidate), lists every real
tagged door/window, and writes them to a schedule table matching the
exact Mark/Level/Type/Width (m)/Height (m) layout
DocumentAnalyzer._read_table already characterizes -- with three
deliberate, documented discrepancies planted for the reconciliation demo:

  - Tag 150173 (a real Level 1 door) is left OFF the schedule -> missing_in_pdf
  - Tag 146596 (a real Level 1 door) has its PDF height altered to 1.91 m
    (true IFC value ~2.01 m) -> dimension_mismatch
  - Tag 146600 is a fabricated row with no matching real IFC element
    (a plausible-looking neighbour of real Level 1 door tags) -> missing_in_ifc

Every other real tagged door/window (35 of them) is listed with its true
IFC Tag/storey/width/height -- correctly matched, not altered.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import ifcopenshell
import ifcopenshell.util.element as el

ROOT = Path(__file__).resolve().parents[1]
IFC_PATH = ROOT / "demo_data" / "projects" / "duplex" / "Duplex_A_20110907.ifc"
OUT_PATH = ROOT / "demo_data" / "projects" / "duplex" / "duplex_schedule.pdf"

STOREY_LABELS = {"Level 1": "L01", "Level 2": "L02", "Roof": "Roof", "T/FDN": "FDN"}

REMOVE_TAGS = {"150173"}
ALTER_HEIGHT = {"146596": 1.91}
FAKE_ROWS = [
    {"tag": "146600", "storey": "L01", "entity_type": "IfcDoor", "width_m": 1.25, "height_m": 2.01},
]

_TABLE_X = [42, 160, 280, 380, 480]
_ROWS_PER_PAGE = 24


def load_real_items() -> list[dict]:
    model = ifcopenshell.open(str(IFC_PATH))
    items = []
    for element in [*model.by_type("IfcDoor"), *model.by_type("IfcWindow")]:
        tag = getattr(element, "Tag", None)
        if not tag:
            continue
        psets = el.get_psets(element, qtos_only=True)
        quantities = next(iter(psets.values()), {}) if psets else {}
        width = quantities.get("Width") or getattr(element, "OverallWidth", None)
        height = quantities.get("Height") or getattr(element, "OverallHeight", None)
        containment = getattr(element, "ContainedInStructure", []) or []
        storey_raw = getattr(containment[0].RelatingStructure, "Name", None) if containment else None
        items.append({
            "tag": str(tag),
            "storey": STOREY_LABELS.get(storey_raw, storey_raw or "?"),
            "entity_type": element.is_a(),
            "width_m": width,
            "height_m": height,
        })
    return items


def build_rows(items: list[dict]) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for item in items:
        if item["tag"] in REMOVE_TAGS:
            continue
        height = ALTER_HEIGHT.get(item["tag"], item["height_m"])
        type_label = "Door" if item["entity_type"] == "IfcDoor" else "Window"
        rows.append((item["tag"], item["storey"], type_label, f"{item['width_m']:.2f}", f"{height:.2f}"))
    for fake in FAKE_ROWS:
        type_label = "Door" if fake["entity_type"] == "IfcDoor" else "Window"
        rows.append((fake["tag"], fake["storey"], type_label, f"{fake['width_m']:.2f}", f"{fake['height_m']:.2f}"))
    rows.sort(key=lambda r: (r[1], r[2], r[0]))
    return rows


def write_schedule(rows: list[tuple[str, str, str, str, str]]) -> None:
    document = fitz.open()
    cover = document.new_page(width=612, height=792)
    cover.insert_text((42, 60), "DUPLEX APARTMENT ENGINEERING SCHEDULE", fontsize=18, fontname="helv", color=(0.08, 0.2, 0.35))
    cover.insert_text((42, 84), "buildingSMART International (BSI) -- real building, CC BY 4.0", fontsize=10, fontname="helv", color=(0.3, 0.3, 0.3))
    cover.insert_textbox(
        fitz.Rect(42, 120, 560, 300),
        "This door/window schedule is ARMIE-generated from the real, unmodified "
        "Duplex_A_20110907.ifc model (BSI (2020) 'Duplex Apartment Test Files,' "
        "buildingSMART International, CC BY 4.0) -- it is not a real project "
        "document. Every Mark below is the model's own real IFC Tag value. See "
        "page 2 onward for the full schedule.",
        fontsize=10.5, fontname="helv", color=(0.15, 0.15, 0.15),
    )
    headers = ("Mark", "Level", "Type", "Width (m)", "Height (m)")
    page_count = (len(rows) + _ROWS_PER_PAGE - 1) // _ROWS_PER_PAGE
    for page_index in range(page_count):
        page = document.new_page(width=612, height=792)
        page.insert_text((42, 40), "DUPLEX DOOR/WINDOW SCHEDULE", fontsize=15, fontname="helv", color=(0.08, 0.2, 0.35))
        page.insert_text((42, 58), f"BSI Duplex Apartment -- real building, CC BY 4.0 source IFC (schedule page {page_index + 1} of {page_count})", fontsize=8, fontname="helv", color=(0.3, 0.3, 0.3))
        chunk = rows[page_index * _ROWS_PER_PAGE:(page_index + 1) * _ROWS_PER_PAGE]
        all_rows = [headers, *chunk]
        y = 95
        for row_index, row in enumerate(all_rows):
            top = y + row_index * 27
            page.draw_rect(fitz.Rect(_TABLE_X[0], top - 15, _TABLE_X[-1] + 60, top + 9), color=(0.4, 0.5, 0.65), fill=(0.92, 0.95, 0.98) if row_index == 0 else (1, 1, 1), width=0.6)
            for col, value in enumerate(row):
                page.insert_text((_TABLE_X[col] + 6, top), value, fontsize=8.5, fontname="helv", color=(0.05, 0.1, 0.2))
    document.save(str(OUT_PATH))


def main() -> None:
    items = load_real_items()
    print(f"Loaded {len(items)} real tagged doors/windows from the IFC.")
    rows = build_rows(items)
    print(f"Writing {len(rows)} schedule rows "
          f"({len(items) - len(REMOVE_TAGS)} real + {len(FAKE_ROWS)} fabricated, "
          f"{len(REMOVE_TAGS)} real item(s) omitted).")
    write_schedule(rows)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
