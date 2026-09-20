"""Owner-reported, 2026-09-19 (live-testing session, real Azure gpt-5-mini):
a missing_in_ifc investigation (SPEC-M17) asked the model to check whether a
candidate IFC window "carries a different tag/mark" than the one under
investigation -- and the model's own answer correctly identified Tag as the
field it needed, then asked the human to go check it, because
get_element_properties never returned it. reconcile_doors_windows already
reads this exact IfcElement.Tag attribute internally
(AgentService._reconciliation_ifc_items) to join against the PDF schedule's
Mark column, but that read was private to reconciliation -- the general-
purpose property_lookup/get_properties tool the model itself calls during a
free-form investigation never surfaced it at all, only GlobalId/ExpressID,
neither of which the PDF side has any equivalent of. Fixed by adding `tag`
to `IfcRepository._compact_element` (used by get_properties' `element` dict
and citation evidence locators alike) and to `_format_ifc_answer`'s
human/model-readable property_lookup text.

Reproduced against the real demo fixture's own known tags (W01 = global_id
1ctyjgDIX8IAhrrKTlC1w0, confirmed live this same session), not a synthetic
value, since the defect was specifically about this real element's real
Tag never reaching the model.
"""

from __future__ import annotations

from pathlib import Path

from app.agent.graph import AgentService
from app.schemas.models import IfcQueryInput, QueryPlan
from app.tools.ifc.repository import IfcRepository

ROOT = Path(__file__).resolve().parents[1]
DEMO_IFC = ROOT / "demo_data" / "armie_demo.ifc"
W01_GLOBAL_ID = "1ctyjgDIX8IAhrrKTlC1w0"  # Level 01 Window 1, Tag "W01"


def test_get_properties_now_returns_the_elements_own_tag():
    repository = IfcRepository(DEMO_IFC)
    query = IfcQueryInput(operation="get_properties", entity_type="IfcWindow", filters={"global_ids": [W01_GLOBAL_ID]})

    result = repository.execute(query)

    assert isinstance(result.value, list) and len(result.value) == 1
    element = result.value[0]["element"]
    assert element["tag"] == "W01"  # previously absent from this dict entirely


def test_property_lookup_citation_evidence_now_carries_the_tag_too():
    """Mirrors reconciliation's own evidence locator (AgentService.
    _synthesize_reconciliation_response), which already includes `tag` --
    a general-purpose property_lookup's own citations previously did not,
    an inconsistency a human reading citations for two different questions
    would notice.
    """
    repository = IfcRepository(DEMO_IFC)
    query = IfcQueryInput(operation="get_properties", entity_type="IfcWindow", filters={"global_ids": [W01_GLOBAL_ID]})

    result = repository.execute(query)

    assert result.evidence and result.evidence[0].locator["tag"] == "W01"


def test_the_rendered_answer_text_states_the_tag_for_a_single_selected_element():
    """The exact text the model would actually see back from its own tool
    call -- not just the underlying dict -- since that is what an
    investigation turn reasons over.
    """
    plan = QueryPlan(
        source="ifc", intent="property_lookup", operation="get_properties", entity_type="IfcWindow",
        filters={"global_ids": [W01_GLOBAL_ID]}, group_by="none", expected_result_shape="properties",
        rationale="test", planning_mode="tool_calling", rule_id="tool:get_element_properties", match_status="complete",
    )
    repository = IfcRepository(DEMO_IFC)
    result = repository.execute(IfcQueryInput(operation="get_properties", entity_type="IfcWindow", filters={"global_ids": [W01_GLOBAL_ID]}))

    answer = AgentService._format_ifc_answer(plan, result.value)

    assert "Tag: W01" in answer
