"""Characterization tests for the CJK/multilingual surface (SPEC-M1 §4.6).

These tests PIN the *current* behaviour of A16 (extensive, hardcoded CJK
special-casing: codepoint scans, Chinese phrase/keyword tables, a hardcoded
Chinese clarification string, and a parallel Chinese answer-rendering
branch) and A17 (ResponseLanguage admits "pt-PT"/"fr"/"es" but no rendering
branch exists for them, so they silently fall through to the English
branch).

They are NOT an endorsement of this design. SPEC-M1 §4.6/18 is explicit:
multilingual handling must not be redesigned, narrowed, or extended in
this milestone -- these tests exist only so a future language-handling
decision (SPEC-M1 §12 REVIEW REQUIRED) changes behaviour deliberately,
with a failing test as the signal, rather than by silent regression.
"""

from __future__ import annotations

from pathlib import Path

from app.agent.graph import AgentService
from app.agent.plan_validation import enforce_grouped_request_contract
from app.agent.router import heuristic_multi_plan, selected_element_plan
from app.config import Settings
from app.schemas.models import MultiQueryPlan, QueryPlan
from app.services import ServiceContainer
from fakes.fake_provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]


def _multi_plan(response_language: str = "zh-CN", corrections=None, requires_clarification: bool = False) -> MultiQueryPlan:
    return MultiQueryPlan(
        response_language=response_language, rationale="r", corrections=corrections or [],
        requires_clarification=requires_clarification,
        subplans=[QueryPlan(subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcDoor", rationale="r", planning_mode="llm", match_status="complete")],
    )


# --- Codepoint scans (router.py, graph.py) ------------------------------------------

def test_codepoint_scan_selects_zh_cn_for_mixed_language_generic_multi_plan() -> None:
    """Pins router.py's `"\\u4e00" <= char <= "\\u9fff"` codepoint scan."""
    multi_plan = heuristic_multi_plan("how many doors 有多少", {}, has_viewer_context=False)
    assert multi_plan is not None
    assert multi_plan.response_language == "zh-CN"


def test_codepoint_scan_selects_en_for_pure_ascii_generic_multi_plan() -> None:
    multi_plan = heuristic_multi_plan("how many doors are there?", {}, has_viewer_context=False)
    assert multi_plan is not None
    assert multi_plan.response_language == "en"


def test_codepoint_scan_selects_zh_cn_for_chinese_deictic_fast_path(tmp_path: Path) -> None:
    """Pins graph.py's use_fast_path codepoint scan (the AgentService._route
    branch that sets selected_language for a deictic/selection-grounded
    request), exercised end-to-end via a Chinese deictic phrase.
    """
    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf", audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence")
    settings.ensure_runtime_directories()
    fake = FakeModelProvider()
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    response = service.invoke(thread_id="cjk-deictic", question="这个是什么", viewer_context={"selected_global_ids": ["1$fakeGlobalId"], "selected_entity_type": "IfcDoor"})
    assert response.execution_metadata.get("response_language") == "zh-CN"


# --- Chinese phrase / grouping-keyword tables --------------------------------------

def test_chinese_deictic_terms_ground_a_selected_element() -> None:
    """Pins router.py's DEICTIC_TERMS Chinese phrase table."""
    context = {"active_entity_ids": ["abc"], "active_entity_type": "IfcDoor"}
    plan = selected_element_plan("这个是什么", context)
    assert plan is not None
    assert plan.rule_id == "selected_element_precedence"


def test_chinese_grouping_markers_enforce_the_grouped_contract() -> None:
    """Pins plan_validation.py's Chinese grouping-keyword table (按楼层/每一层/etc)."""
    plan = QueryPlan(subtask_id="task_1", source="ifc", intent="count", operation="count", entity_type="IfcWindow", group_by="none", rationale="r", planning_mode="llm", match_status="complete")
    multi_plan = MultiQueryPlan(response_language="zh-CN", rationale="r", subplans=[plan])
    corrected, events = enforce_grouped_request_contract(multi_plan, "按楼层统计窗户数量")
    assert corrected.subplans[0].operation == "group_by"
    assert corrected.subplans[0].group_by == "storey"
    assert events


# --- Hardcoded Chinese clarification string (graph.py) ------------------------------

def test_hardcoded_chinese_board_ambiguity_clarification(tmp_path: Path) -> None:
    """Pins the hardcoded Chinese clarification string in
    AgentService._resolve_context for a bare "board" reference, which is
    resolved before any model call or routing.
    """
    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf", audit_store_path=tmp_path / "audit.jsonl", evidence_dir=tmp_path / "evidence")
    settings.ensure_runtime_directories()
    fake = FakeModelProvider()  # no scripted responses: this path must not call the model
    container = ServiceContainer(settings, text_provider_factory=lambda s: fake, vision_provider_factory=lambda s: fake)
    service = AgentService(container)
    response = service.invoke(thread_id="cjk-board", question="这张图里的板有多少", viewer_context=None)
    assert response.disposition.value == "clarification_required"
    assert response.answer_markdown == "我需要知道你指的是哪一种板或数据源。请明确 IFC 楼板数量，或指定工程图纸中的配电板/回路。"


# --- Chinese answer-rendering branches (count, group_by, get_properties) ------------

def test_chinese_rendering_branch_for_count() -> None:
    subresults = [{"plan": {"source": "ifc", "operation": "count", "entity_type": "IfcDoor"}, "answer": "The project contains **4** doors.", "disposition": "answered"}]
    answer = AgentService._natural_answer(_multi_plan(), subresults)
    assert answer == "这个项目中共有 **4** 扇门。"


def test_chinese_rendering_branch_for_group_by() -> None:
    subresults = [{
        "plan": {"source": "ifc", "operation": "group_by", "entity_type": "IfcWindow", "postprocess": None},
        "answer": "irrelevant", "disposition": "answered",
        "result_value": {"Level 01": 2, "Level 02": 2},
    }]
    answer = AgentService._natural_answer(_multi_plan(), subresults)
    assert answer == "各层窗户数量：**Level 01**：**2**；**Level 02**：**2**。"


def test_chinese_rendering_branch_for_get_properties() -> None:
    subresults = [{
        "plan": {"source": "ifc", "operation": "get_properties", "entity_type": "IfcDoor"},
        "answer": "irrelevant", "disposition": "answered",
        "result_value": [{"element": {"entity_type": "IfcDoor", "name": "D-101"}, "storey": "Level 01"}],
    }]
    answer = AgentService._natural_answer(_multi_plan(), subresults)
    assert "当前选中的是一个门" in answer
    assert "**IfcDoor**" in answer
    assert "**Level 01**" in answer


def test_chinese_correction_prefix_is_prepended_when_uncorrected_clarification_absent() -> None:
    subresults = [{"plan": {"source": "ifc", "operation": "count", "entity_type": "IfcDoor"}, "answer": "The project contains **4** doors.", "disposition": "answered"}]
    answer = AgentService._natural_answer(_multi_plan(corrections=[{"correction": "x"}]), subresults)
    assert answer.startswith("我理解你是在问建筑模型中的对应构件。")


# --- A17: pt-PT/fr/es admitted by the schema but silently render as English --------

def test_pt_pt_response_language_falls_through_to_the_english_branch() -> None:
    """Pins A17: ResponseLanguage.code admits "pt-PT" (and "fr", "es"), but no
    rendering branch exists for it in AgentService._natural_answer -- it
    silently renders identically to English. Not fixed or extended here
    (SPEC-M1 §4.6/18, OD-8): a later milestone decides whether to narrow,
    implement, or redesign the language contract (SPEC-M1 §12).
    """
    subresults = [{"plan": {"source": "ifc", "operation": "count", "entity_type": "IfcDoor"}, "answer": "The project contains **4** doors.", "disposition": "answered"}]
    answer = AgentService._natural_answer(_multi_plan(response_language="pt-PT"), subresults)
    assert answer == "The project contains **4** doors."
