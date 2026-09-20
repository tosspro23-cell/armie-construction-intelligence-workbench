"""Independent-review finding, 2026-09-19 (live-testing session): SPEC-M17's
`AgentService._extract_proposed_dimension` could fabricate a proposed
dimension from a number that had nothing to do with the dimension being
investigated at all, and marked it `verified` -- confidently wrong, not
merely ungrounded. Two distinct, independently reproduced bugs:

1. An investigation whose actual tool call was `count_elements(IfcDoor)`
   (returning 4) with the model's whole answer being "There are 4 doors."
   persisted `proposed_width_m = proposed_height_m = 4.0` -- the function's
   old "exactly one number that isn't either known value -> treat it as a
   genuinely new proposed value" fallback had no way to tell a real
   third-source measurement apart from an unrelated real number that
   simply happened to be the answer's only one.
2. An answer that explicitly rejects the IFC value ("Do not use the IFC
   value of 1.75m; the correct height is still unknown.") still had 1.75
   extracted as the proposed height -- the old substring/number-match
   check had no concept of negation.

Fixed by removing the "guess from leftover numbers" fallback entirely
(no version of it can safely distinguish the two cases above from free
text alone) and adding a narrow rejection-phrase check immediately before
a candidate number.
"""

from __future__ import annotations

from app.agent.graph import AgentService


def test_an_unrelated_tool_result_number_is_never_adopted_as_a_proposed_dimension():
    """The `count_elements(IfcDoor)` repro exactly as it happened live."""
    answer = "There are 4 doors."

    width = AgentService._extract_proposed_dimension(answer, ifc_value=1.2, pdf_value=1.2)
    height = AgentService._extract_proposed_dimension(answer, ifc_value=1.75, pdf_value=1.7)

    assert width is None
    assert height is None


def test_a_number_explicitly_rejected_in_the_text_is_not_extracted():
    answer = "Do not use the IFC value of 1.75m; the correct height is still unknown."

    height = AgentService._extract_proposed_dimension(answer, ifc_value=1.75, pdf_value=1.7)

    assert height is None


def test_a_genuinely_endorsed_value_is_still_extracted():
    """The fix must not throw out the working case along with the bug --
    an answer that actually commits to one of the two known values (and
    never mentions the other one's actual number) must still surface it.
    """
    answer = "The IFC height of 1.75m is confirmed correct based on the model's own recorded properties."

    height = AgentService._extract_proposed_dimension(answer, ifc_value=1.75, pdf_value=1.7)

    assert height == 1.75


def test_both_known_values_discussed_without_committing_yields_no_proposal():
    answer = "Both the IFC's 1.75m and the PDF's 1.70m appear in the sources; I can't determine which is correct."

    height = AgentService._extract_proposed_dimension(answer, ifc_value=1.75, pdf_value=1.7)

    assert height is None


def test_neither_known_value_present_yields_no_proposal_even_when_one_other_number_is():
    """Direct pin of the removed fallback: a lone unrelated number must
    never become the proposed value, regardless of how the text is
    phrased or how confident it sounds.
    """
    answer = "Based on my analysis, the correct value is 4.0 meters."

    height = AgentService._extract_proposed_dimension(answer, ifc_value=1.75, pdf_value=1.7)

    assert height is None
