"""SPEC-M13 §A: a real-tag-grounded multi-document corpus for the Duplex
Apartment Dataset Pack project, mirroring SPEC-M6's own proven
precision-collision/recall-failure fixture design (`generate_demo_data.py`'s
`make_document_corpus`) rather than reinventing it -- reskinned with
Duplex's own real storeys and real door/window Tags for grounding.

Separate from `generate_duplex_schedule.py` (the real door/window
schedule with its own planted reconciliation discrepancies, D-036/D-037) --
this corpus is additive, a different synthetic domain (electrical
distribution schedules, hardware spec sheets, RFIs, meeting minutes)
layered alongside it, never touching that file or its fixture.

Filenames are all `duplex_`-prefixed: the shared Azure AI Search index
(`scripts/index_document_corpus.py`) has no project-scoping field, so a
basename collision with another project's document would silently merge
two unrelated documents under one search-index id.
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

IFC_PATH = ROOT / "demo_data" / "projects" / "duplex" / "Duplex_A_20110907.ifc"
CORPUS_DIR = ROOT / "demo_data" / "projects" / "duplex" / "corpus"
REGISTRY_PATH = ROOT / "demo_data" / "projects_registry.json"

# Tags already involved in duplex_schedule.pdf's own planted reconciliation
# discrepancies (generate_duplex_schedule.py) -- excluded here so this
# corpus's own "correct" values are never ambiguous against that separate
# fixture's deliberately-altered ones.
_RECONCILIATION_TAGS = {"150173", "146596", "146600"}


def _safe_tags(model, entity_type: str, count: int, skip: set[str]) -> list[str]:
    tags = sorted(
        (str(e.Tag) for e in model.by_type(entity_type) if e.Tag and str(e.Tag) not in _RECONCILIATION_TAGS and str(e.Tag) not in skip),
        key=int,
    )
    return tags[:count]


def make_distribution_schedules() -> None:
    """Precision-collision fixture, mirroring SPEC-M6's `schedule_l2_east.pdf`/
    `schedule_l2_west.pdf` exactly: "Panel-A" appears in the Level 1 and
    Level 2 schedules with two different Connected Load values -- a
    question naming only "Panel-A" is genuinely answerable from either,
    same as the proven `demo` fixture.
    """
    headers = ("Board", "Connected Load (kW)", "Diversity Factor")
    _make_table_document(
        CORPUS_DIR / "duplex_schedule_level1.pdf",
        "DUPLEX LEVEL 1 DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-A", "22.40", "0.68"), ("Panel-C", "14.10", "0.60")],
    )
    _make_table_document(
        CORPUS_DIR / "duplex_schedule_level2.pdf",
        "DUPLEX LEVEL 2 DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-A", "19.85", "0.65"), ("Panel-D", "16.30", "0.62")],
    )
    _make_table_document(
        CORPUS_DIR / "duplex_schedule_roof.pdf",
        "DUPLEX ROOF DISTRIBUTION SCHEDULE", "ARMIE-generated synthetic fixture · not a real project document",
        headers, [("Panel-G", "8.20", "0.55"), ("Panel-H", "6.75", "0.50")],
    )


def make_spec_sheets(model) -> tuple[list[str], list[str]]:
    """Door/window hardware spec sheets grounded in real IFC Tags -- the
    Item column uses each element's own real Tag, not a fabricated code,
    so a question like "what is the fire rating for door 146678" resolves
    against the same identifier the real building's own schedule uses.
    """
    door_tags = _safe_tags(model, "IfcDoor", 3, skip=set())
    window_tags = _safe_tags(model, "IfcWindow", 3, skip=set())

    door_ratings = ["90 min", "60 min", "20 min"]
    door_materials = ["Hollow Metal", "Wood", "Aluminum"]
    _make_table_document(
        CORPUS_DIR / "duplex_door_spec_sheet.pdf",
        "DUPLEX DOOR HARDWARE SPECIFICATION", "ARMIE-generated synthetic fixture · real IFC Tag identifiers, not a real project document",
        ("Item", "Fire Rating", "Frame Material"),
        list(zip(door_tags, door_ratings, door_materials)),
    )

    window_glazing = ["Double IGU", "Triple IGU", "Double IGU"]
    window_materials = ["Aluminum", "Vinyl", "Wood"]
    _make_table_document(
        CORPUS_DIR / "duplex_window_spec_sheet.pdf",
        "DUPLEX WINDOW GLAZING SPECIFICATION", "ARMIE-generated synthetic fixture · real IFC Tag identifiers, not a real project document",
        ("Item", "Glazing Type", "Frame Material"),
        list(zip(window_tags, window_glazing, window_materials)),
    )
    return door_tags, window_tags


def make_rfi_log(recall_tag: str) -> None:
    """Recall-failure fixture, mirroring SPEC-M6's `rfi_log_047.pdf`: a
    real, specific value stated only in prose, tied to a real window Tag,
    appearing in no table anywhere in this corpus -- resolvable only by
    Azure AI Search's semantic retrieval, never by native_lookup's
    keyword/table extraction.
    """
    _make_narrative_document(
        CORPUS_DIR / "duplex_rfi_log_001.pdf",
        f"RFI-001 -- WINDOW SILL FLASHING FIELD CONDITION AT TAG {recall_tag}", "ARMIE-generated synthetic fixture · not a real project document",
        [
            f"Question: The window represented by IFC Tag {recall_tag} on Level 2 was found during framing "
            "inspection to have a rough opening that does not match the issued glazing specification.",
            f"Response: A field measurement confirmed the as-built rough opening width for Tag {recall_tag} "
            "is 1.35 m, requiring a revised sill flashing detail. This value is not yet reflected in any "
            "issued schedule or specification sheet; the window glazing specification will be reissued at "
            "the next drawing revision.",
        ],
    )


def make_meeting_minutes() -> None:
    _make_narrative_document(
        CORPUS_DIR / "duplex_meeting_minutes_2026_02_10.pdf",
        "DUPLEX APARTMENT PROGRESS MEETING MINUTES -- 2026-02-10", "ARMIE-generated synthetic fixture · not a real project document",
        [
            "Attendees: General Contractor, Architect, MEP Engineer, Owner's Representative.",
            "Electrical: Distribution schedules for Level 1 and Level 2 are issued; the Roof-level panel "
            "schedule is still in coordination and expected to issue at the next milestone.",
            "Action items: Architect to confirm door and window hardware package assignments against the "
            "issued specification sheets by the next meeting.",
        ],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def update_registry(pdf_files: list[str]) -> None:
    """SPEC-M9 OD-41: adding files to an existing project's manifest is a
    content change, so `source_set_id` bumps (duplex-v1 -> duplex-v2) --
    never silently edited behind the existing id, matching the same
    v1->v2 bump `digitalhub` already went through when its schedule was
    added.
    """
    registry = json.loads(REGISTRY_PATH.read_text())
    entry = registry["duplex"]
    entry["source_set_id"] = "duplex-v2"
    for pdf_file in pdf_files:
        relative = f"corpus/{pdf_file}"
        if relative not in entry["pdf_files"]:
            entry["pdf_files"].append(relative)
        entry["files"][relative] = {"content_sha256": _sha256(CORPUS_DIR / pdf_file)}
    registry["duplex"] = entry
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


def main() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    model = ifcopenshell.open(str(IFC_PATH))
    make_distribution_schedules()
    door_tags, window_tags = make_spec_sheets(model)
    recall_tag = _safe_tags(model, "IfcWindow", 4, skip=set(window_tags))[-1]
    make_rfi_log(recall_tag)
    make_meeting_minutes()
    pdf_files = sorted(p.name for p in CORPUS_DIR.glob("duplex_*.pdf"))
    update_registry(pdf_files)
    print(f"Wrote {len(pdf_files)} corpus documents to {CORPUS_DIR}")
    print(f"Door spec sheet Tags: {door_tags}")
    print(f"Window spec sheet Tags: {window_tags}")
    print(f"RFI recall-failure Tag: {recall_tag}")


if __name__ == "__main__":
    main()
