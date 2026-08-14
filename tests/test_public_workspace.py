from pathlib import Path

from app.config import Settings
from app.schemas.models import IfcQueryInput
from app.tools.ifc.repository import IfcRepository


ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_ifc_is_real_and_queryable() -> None:
    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf")
    repository = IfcRepository(settings.ifc_path)
    assert repository.available
    assert len(repository.model.by_type("IfcBuildingStorey")) == 2
    assert repository.execute(IfcQueryInput(operation="count", entity_type="IfcDoor")).value == 4
    grouped = repository.execute(IfcQueryInput(operation="group_by", entity_type="IfcWindow", group_by="storey"))
    assert grouped.value == {"Level 01": 2, "Level 02": 2}


def test_synthetic_quantity_is_unit_normalized_and_evidence_bearing() -> None:
    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf")
    repository = IfcRepository(settings.ifc_path)
    result = repository.execute(IfcQueryInput(operation="aggregate_quantity", entity_type="IfcWindow", measure="height", aggregation="max"))
    assert result.value["value_m"] == 1.75
    assert result.value["quantity_path"] == "Qto_WindowBaseQuantities.Height"
    assert result.evidence
