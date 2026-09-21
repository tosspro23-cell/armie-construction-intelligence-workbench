"""D-072 (2026-09-21, found live in the very next real investigation after
D-070 deployed, real Azure gpt-5-mini against RWTH DigitalHub): once D-070
fixed the wrong-*entity-type* mistake, the same investigation went on to
call `get_element_properties(entity_type="IfcWindow", global_ids=
["2543664"])` -- passing the finding's own real Tag ("2543664") as a
`global_ids` value. `global_ids` matches `IfcElement.GlobalId` (the model's
internal GUID, e.g. "0ehNcYPbH3JQicvZQLHP24"), never `IfcElement.Tag` (the
human-facing identifier a PDF schedule row or a finding's own tag actually
uses) -- so this call was guaranteed to match nothing, on every real
building, regardless of which element it meant. The only other path (an
unfiltered `entity_type` query) gets capped to a small sample for a real
building's element count (D-067's `_LARGE_LIST_SAMPLE_SIZE`), so whether
the target tag happened to land in that sample was pure luck; it didn't,
and the investigation exhausted its tool-call budget before genuinely
giving up `inconclusive`.

Fixed by adding a `tags` filter to `get_element_properties`, matching
`element.Tag` directly (`IfcRepository._matching_elements`) -- the same
attribute `_reconciliation_ifc_items` already keys on, and the same
established real-fixture pattern `test_ifc_element_tag_exposure.py` uses
(W01 = global_id 1ctyjgDIX8IAhrrKTlC1w0, Tag "W01").
"""

from __future__ import annotations

from pathlib import Path

from app.agent.tools import build_plan_from_tool_call
from app.schemas.models import IfcQueryInput
from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DEMO_IFC = ROOT / "demo_data" / "armie_demo.ifc"
W01_GLOBAL_ID = "1ctyjgDIX8IAhrrKTlC1w0"  # Level 01 Window 1, Tag "W01"


def test_get_properties_by_tag_finds_the_correct_element():
    repository = IfcRepository(DEMO_IFC)
    query = IfcQueryInput(operation="get_properties", entity_type="IfcWindow", filters={"tags": ["W01"]})

    result = repository.execute(query)

    assert isinstance(result.value, list) and len(result.value) == 1
    element = result.value[0]["element"]
    assert element["tag"] == "W01"
    assert element["global_id"] == W01_GLOBAL_ID


def test_passing_a_tag_as_global_ids_finds_nothing_the_exact_defect_this_closes():
    """Reproduces the live-observed mistake directly: a finding's own Tag
    passed as `global_ids` matches nothing, because GlobalId and Tag are
    different attributes -- confirming why the pre-fix investigation's
    `get_element_properties(global_ids=["2543664"])` call was guaranteed
    to fail regardless of which element it meant, not a fluke of that one
    turn.
    """
    repository = IfcRepository(DEMO_IFC)
    query = IfcQueryInput(operation="get_properties", entity_type="IfcWindow", filters={"global_ids": ["W01"]})

    result = repository.execute(query)

    assert result.value == []


def test_build_plan_from_tool_call_wires_tags_into_the_query_plan():
    plan = build_plan_from_tool_call("get_element_properties", {"entity_type": "IfcWindow", "tags": ["W01"]})

    assert plan.filters == {"tags": ["W01"]}


def test_build_plan_from_tool_call_can_combine_tags_and_global_ids():
    plan = build_plan_from_tool_call(
        "get_element_properties", {"entity_type": "IfcWindow", "tags": ["W01"], "global_ids": [W01_GLOBAL_ID]},
    )

    assert plan.filters == {"global_ids": [W01_GLOBAL_ID], "tags": ["W01"]}
