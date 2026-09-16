"""SPEC-M13 §A: a real-tag-grounded multi-document corpus for the
RWTH-E3D DigitalHub Dataset Pack project -- see
`generate_duplex_corpus.py`'s own header for the shared design rationale
(mirrors SPEC-M6's proven precision-collision/recall-failure fixture
design, reskinned per building). Separate from
`generate_digitalhub_schedule.py` (the real door/window schedule with its
own planted reconciliation discrepancies, D-035/D-036) -- additive, never
touching that file or its fixture.

Filenames are all `digitalhub_`-prefixed for the same shared-index
collision-avoidance reason as the Duplex corpus.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import ifcopenshell

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_demo_data import (  # noqa: E402
    _make_narrative_document,
    _make_table_document,
)

IFC_PATH = ROOT / "demo_data" / "projects" / "digitalhub" / "DigitalHub_FM-ARC_v2.ifc"
CORPUS_DIR = ROOT / "demo_data" / "projects" / "digitalhub" / "corpus"
REGISTRY_PATH = ROOT / "demo_data" / "projects_registry.json"

# Tags already involved in digitalhub_schedule.pdf's own planted
# reconciliation discrepancies (generate_digitalhub_schedule.py).
_RECONCILIATION_TAGS = {"2434145", "2432934", "2553900"}


def _safe_tags(model, entity_type: str, count: int, skip: set[str]) -> list[str]:
    tags = sorted(
        (str(e.Tag) for e in model.by_type(entity_type) if e.Tag and str(e.Tag) not in _RECONCILIATION_TAGS and str(e.Tag) not in skip),
        key=int,
    )
    return tags[:count]


def make_distribution_schedules() -> None:
    """Precision-collision fixture (SPEC-M6's proven design): "Panel-A"
    appears in the B01_OKRD and E00_OKRD schedules with two different
    Connected Load values.
    """
    headers = ("Board", "Connected Load (kW)", "Diversity Factor")
    _make_table_document(
        CORPUS_DIR / "digitalhub_schedule_b01.pdf",
        "DIGITALHUB B01_OKRD DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-A", "55.00", "0.75"), ("Panel-M", "30.20", "0.70")],
    )
    _make_table_document(
        CORPUS_DIR / "digitalhub_schedule_e00.pdf",
        "DIGITALHUB E00_OKRD DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-A", "48.60", "0.72"), ("Panel-N", "38.40", "0.68")],
    )
    _make_table_document(
        CORPUS_DIR / "digitalhub_schedule_e01.pdf",
        "DIGITALHUB E01_OKRD DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-P", "42.10", "0.74"), ("Panel-Q", "29.85", "0.66")],
    )


def make_spec_sheets(model) -> tuple[list[str], list[str]]:
    door_tags = _safe_tags(model, "IfcDoor", 3, skip=set())
    window_tags = _safe_tags(model, "IfcWindow", 3, skip=set())

    door_ratings = ["90 min", "60 min", "20 min"]
    door_materials = ["Hollow Metal", "Aluminum", "Wood"]
    _make_table_document(
        CORPUS_DIR / "digitalhub_door_spec_sheet.pdf",
        "DIGITALHUB DOOR HARDWARE SPECIFICATION", "ARMIE-generated synthetic fixture · real IFC Tag identifiers, not a real project document",
        ("Item", "Fire Rating", "Frame Material"),
        list(zip(door_tags, door_ratings, door_materials)),
    )

    window_glazing = ["Double IGU", "Triple IGU", "Laminated"]
    window_materials = ["Aluminum", "Vinyl", "Aluminum"]
    _make_table_document(
        CORPUS_DIR / "digitalhub_window_spec_sheet.pdf",
        "DIGITALHUB WINDOW GLAZING SPECIFICATION", "ARMIE-generated synthetic fixture · real IFC Tag identifiers, not a real project document",
        ("Item", "Glazing Type", "Frame Material"),
        list(zip(window_tags, window_glazing, window_materials)),
    )
    return door_tags, window_tags


def make_rfi_log(recall_tag: str) -> None:
    _make_narrative_document(
        CORPUS_DIR / "digitalhub_rfi_log_001.pdf",
        f"RFI-001 -- WINDOW SILL FLASHING FIELD CONDITION AT TAG {recall_tag}", "ARMIE-generated synthetic fixture · not a real project document",
        [
            f"Question: The window represented by IFC Tag {recall_tag} on E01_OKRD was found during facade "
            "inspection to have a rough opening that does not match the issued glazing specification.",
            f"Response: A field measurement confirmed the as-built rough opening width for Tag {recall_tag} "
            "is 1.42 m, requiring a revised sill flashing detail. This value is not yet reflected in any "
            "issued schedule or specification sheet; the window glazing specification will be reissued at "
            "the next drawing revision.",
        ],
    )


def make_meeting_minutes() -> None:
    _make_narrative_document(
        CORPUS_DIR / "digitalhub_meeting_minutes_2026_02_10.pdf",
        "DIGITALHUB PROGRESS MEETING MINUTES -- 2026-02-10", "ARMIE-generated synthetic fixture · not a real project document",
        [
            "Attendees: General Contractor, Architect, MEP Engineer, Owner's Representative.",
            "Electrical: Distribution schedules for B01_OKRD and E00_OKRD are issued; the E01_OKRD panel "
            "schedule is still in coordination and expected to issue at the next milestone.",
            "Action items: Architect to confirm door and window hardware package assignments against the "
            "issued specification sheets by the next meeting.",
        ],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def update_registry(pdf_files: list[str]) -> None:
    """SPEC-M9 OD-41: bumps `source_set_id` (digitalhub-v2 -> digitalhub-v3)
    since this adds files to the manifest, matching the same discipline
    `generate_duplex_corpus.py` follows.
    """
    registry = json.loads(REGISTRY_PATH.read_text())
    entry = registry["digitalhub"]
    entry["source_set_id"] = "digitalhub-v3"
    for pdf_file in pdf_files:
        relative = f"corpus/{pdf_file}"
        if relative not in entry["pdf_files"]:
            entry["pdf_files"].append(relative)
        entry["files"][relative] = {"content_sha256": _sha256(CORPUS_DIR / pdf_file)}
    registry["digitalhub"] = entry
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


def main() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    model = ifcopenshell.open(str(IFC_PATH))
    make_distribution_schedules()
    door_tags, window_tags = make_spec_sheets(model)
    recall_tag = _safe_tags(model, "IfcWindow", 4, skip=set(window_tags))[-1]
    make_rfi_log(recall_tag)
    make_meeting_minutes()
    pdf_files = sorted(p.name for p in CORPUS_DIR.glob("digitalhub_*.pdf"))
    update_registry(pdf_files)
    print(f"Wrote {len(pdf_files)} corpus documents to {CORPUS_DIR}")
    print(f"Door spec sheet Tags: {door_tags}")
    print(f"Window spec sheet Tags: {window_tags}")
    print(f"RFI recall-failure Tag: {recall_tag}")


if __name__ == "__main__":
    main()
