"""One-off generator for the DigitalHub door/window schedule PDF.

Not part of scripts/generate_demo_data.py (that script's own header notes
it no longer reproduces the committed synthetic fixtures byte-for-byte;
this is a separate, real-building fixture with its own provenance, per
D-035/D-036). Reads the real, unmodified DigitalHub_FM-ARC_v2.ifc directly
via ifcopenshell, lists every real tagged door/window, and writes them to
a multi-page schedule table matching the exact Mark/Level/Type/Width (m)/
Height (m) layout DocumentAnalyzer._read_table already characterizes --
with four deliberate, documented discrepancies planted for the
reconciliation demo (D-036):

  - Tag 2434145 (a real B01 door) is left OFF the schedule -> missing_in_pdf
  - Tag 2432934 (a real B01 door) has its PDF height altered to 1.945 m
    (true IFC value 2.045 m) -> dimension_mismatch
  - Tag 2543664 (a real E00 window) has its PDF width altered to 5.80 m
    (true IFC value 6.0 m) -> dimension_mismatch
  - Tag 2553900 is a fabricated row with no matching real IFC element
    (a plausible-looking neighbour of real E01 door tags) -> missing_in_ifc

Every other real tagged door/window (107 of them) is listed with its true
IFC Tag/storey/width/height -- correctly matched, not altered.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import ifcopenshell
import ifcopenshell.util.element as el

ROOT = Path(__file__).resolve().parents[1]
IFC_PATH = ROOT / "demo_data" / "projects" / "digitalhub" / "DigitalHub_FM-ARC_v2.ifc"
OUT_PATH = ROOT / "demo_data" / "projects" / "digitalhub" / "digitalhub_schedule.pdf"

STOREY_LABELS = {"B01_OKRD": "B01", "E00_OKRD": "E00", "E01_OKRD": "E01"}

REMOVE_TAGS = {"2434145"}
ALTER_HEIGHT = {"2432934": 1.945}
ALTER_WIDTH = {"2543664": 5.80}
FAKE_ROWS = [
    {"tag": "2553900", "storey": "E01", "entity_type": "IfcDoor", "width_m": 1.09, "height_m": 2.045},
]

_TABLE_X = [42, 160, 280, 380, 480]
_ROWS_PER_PAGE = 24


def load_real_items() -> list[dict]:
    model = ifcopenshell.open(IFC_PATH)
    items = []
    for element in [*model.by_type("IfcDoor"), *model.by_type("IfcWindow")]:
        tag = getattr(element, "Tag", None)
        if not tag:
            continue
        psets = el.get_psets(element, qtos_only=True)
        quantities = next(iter(psets.values()), {}) if psets else {}
        containment = getattr(element, "ContainedInStructure", []) or []
        storey_raw = getattr(containment[0].RelatingStructure, "Name", None) if containment else None
        items.append({
            "tag": str(tag),
            "storey": STOREY_LABELS.get(storey_raw, storey_raw or "?"),
            "entity_type": element.is_a(),
            "width_m": quantities.get("Width"),
            "height_m": quantities.get("Height"),
        })
    return items


def build_rows(items: list[dict]) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for item in items:
        if item["tag"] in REMOVE_TAGS:
            continue
        width = ALTER_WIDTH.get(item["tag"], item["width_m"])
        height = ALTER_HEIGHT.get(item["tag"], item["height_m"])
        type_label = "Door" if item["entity_type"] == "IfcDoor" else "Window"
        rows.append((item["tag"], item["storey"], type_label, f"{width:.2f}", f"{height:.2f}"))
    for fake in FAKE_ROWS:
        type_label = "Door" if fake["entity_type"] == "IfcDoor" else "Window"
        rows.append((fake["tag"], fake["storey"], type_label, f"{fake['width_m']:.2f}", f"{fake['height_m']:.2f}"))
    rows.sort(key=lambda r: (r[1], r[2], r[0]))
    return rows


def write_schedule(rows: list[tuple[str, str, str, str, str]]) -> None:
    document = fitz.open()
    # Page 1 is a cover/notes page, not part of the table -- keeps this
    # fixture on the same "schedule table starts at page 2" convention
    # _reconciliation_pdf_items defaults to (matching armie_demo_schedule.pdf),
    # so no per-project page-number override is needed anywhere.
    cover = document.new_page(width=612, height=792)
    cover.insert_text((42, 60), "DIGITALHUB ENGINEERING SCHEDULE", fontsize=18, fontname="helv", color=(0.08, 0.2, 0.35))
    cover.insert_text((42, 84), "RWTH Aachen University E3D Institute -- real building, MIT License", fontsize=10, fontname="helv", color=(0.3, 0.3, 0.3))
    cover.insert_textbox(
        fitz.Rect(42, 120, 560, 300),
        "This door/window schedule is ARMIE-generated from the real, unmodified "
        "DigitalHub_FM-ARC_v2.ifc model (RWTH-E3D, MIT License) -- it is not a "
        "real project document. Every Mark below is the model's own real IFC "
        "Tag value. See page 2 onward for the full schedule.",
        fontsize=10.5, fontname="helv", color=(0.15, 0.15, 0.15),
    )
    headers = ("Mark", "Level", "Type", "Width (m)", "Height (m)")
    page_count = (len(rows) + _ROWS_PER_PAGE - 1) // _ROWS_PER_PAGE
    for page_index in range(page_count):
        page = document.new_page(width=612, height=792)
        page.insert_text((42, 40), "DIGITALHUB DOOR/WINDOW SCHEDULE", fontsize=15, fontname="helv", color=(0.08, 0.2, 0.35))
        page.insert_text((42, 58), f"RWTH-E3D DigitalHub -- real building, MIT-licensed source IFC (schedule page {page_index + 1} of {page_count})", fontsize=8, fontname="helv", color=(0.3, 0.3, 0.3))
        chunk = rows[page_index * _ROWS_PER_PAGE:(page_index + 1) * _ROWS_PER_PAGE]
        all_rows = [headers, *chunk]
        y = 95
        for row_index, row in enumerate(all_rows):
            top = y + row_index * 27
            page.draw_rect(fitz.Rect(_TABLE_X[0], top - 15, _TABLE_X[-1] + 60, top + 9), color=(0.4, 0.5, 0.65), fill=(0.92, 0.95, 0.98) if row_index == 0 else (1, 1, 1), width=0.6)
            for col, value in enumerate(row):
                page.insert_text((_TABLE_X[col] + 6, top), value, fontsize=8.5 if row_index else 8.5, fontname="helv", color=(0.05, 0.1, 0.2))
    document.save(OUT_PATH)


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
