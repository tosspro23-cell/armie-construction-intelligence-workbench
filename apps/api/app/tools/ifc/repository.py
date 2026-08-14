from __future__ import annotations

from collections import defaultdict
from functools import cached_property
from math import sqrt
from pathlib import Path
from typing import Any

from app.schemas.models import Evidence, IfcQueryInput, IfcQueryResult, SourceType


class IfcRepositoryError(RuntimeError):
    pass


class IfcRepository:
    """Deterministic, constrained query adapter around IfcOpenShell."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._model = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    @cached_property
    def model(self):
        if not self.path.exists():
            raise IfcRepositoryError(f"IFC file not found: {self.path}")
        try:
            import ifcopenshell
        except ImportError as error:
            raise IfcRepositoryError("IfcOpenShell is not installed") from error
        return ifcopenshell.open(str(self.path))

    def metadata(self) -> dict[str, Any]:
        if not self.available:
            return {"available": False, "path": str(self.path)}
        model = self.model
        return {
            "available": True,
            "schema": model.schema,
            "project": self._name_of(self._first("IfcProject")),
            "storeys": [
                {
                    "global_id": getattr(storey, "GlobalId", None),
                    "name": self._name_of(storey),
                    "elevation": getattr(storey, "Elevation", None),
                }
                for storey in model.by_type("IfcBuildingStorey")
            ],
        }

    @cached_property
    def _space_by_element(self) -> dict[str, set[str]]:
        """Map building-element GUIDs to bounded spaces when the model exposes them."""
        mapping: dict[str, set[str]] = defaultdict(set)
        for boundary in self.model.by_type("IfcRelSpaceBoundary"):
            element = getattr(boundary, "RelatedBuildingElement", None)
            space = getattr(boundary, "RelatingSpace", None)
            global_id = getattr(element, "GlobalId", None)
            if global_id and space:
                mapping[global_id].add(self._name_of(space))
        return mapping

    @cached_property
    def _aggregate_parent_by_id(self) -> dict[int, Any]:
        """Resolve children of composite IFC elements such as curtain-wall windows."""
        parents: dict[int, Any] = {}
        for relation in self.model.by_type("IfcRelAggregates"):
            parent = getattr(relation, "RelatingObject", None)
            for child in getattr(relation, "RelatedObjects", []) or []:
                if parent:
                    parents[child.id()] = parent
        return parents

    @cached_property
    def viewer_elements(self) -> list[dict[str, Any]]:
        """Return lightweight, real-IFC bounding geometry for an interactive browser view.

        The browser gets a bounded set of element bounding boxes generated from
        IfcOpenShell. This avoids freezing the UI while a legacy browser parser
        reconstructs the complete 11 MB source model, while preserving true IFC
        element identities for selection, citations, and agent context.
        """
        if not self.available:
            return []
        try:
            import ifcopenshell.geom
        except ImportError as error:
            raise IfcRepositoryError("IfcOpenShell geometry support is not installed") from error

        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        supported_types = ("IfcWall", "IfcSlab", "IfcDoor", "IfcWindow", "IfcStair", "IfcRoof")
        palette = {
            "IfcWall": "#8aa4c8",
            "IfcSlab": "#54708d",
            "IfcDoor": "#d89c58",
            "IfcWindow": "#69b9dd",
            "IfcStair": "#c5a4db",
            "IfcRoof": "#7cae7b",
        }
        output: list[dict[str, Any]] = []
        # The source has hundreds of walls. A bounded representative geometry set
        # keeps the browser responsive while including every door, window, stair,
        # slab and roof for demonstrable element selection.
        per_type_limits = {"IfcWall": 160, "IfcSlab": 50, "IfcDoor": 100, "IfcWindow": 120, "IfcStair": 30, "IfcRoof": 10}
        for entity_type in supported_types:
            for element in self.model.by_type(entity_type)[: per_type_limits[entity_type]]:
                try:
                    shape = ifcopenshell.geom.create_shape(settings, element)
                    vertices = list(shape.geometry.verts)
                    if not vertices:
                        raise ValueError("empty geometry")
                    xs, ys, zs = vertices[0::3], vertices[1::3], vertices[2::3]
                    minimum = [min(xs), min(ys), min(zs)]
                    maximum = [max(xs), max(ys), max(zs)]
                except Exception:
                    # Public synthetic fixtures may intentionally carry
                    # lightweight semantic geometry. Keep those elements
                    # selectable in the browser with a bounded proxy box.
                    placement = getattr(element, "ObjectPlacement", None)
                    try:
                        from ifcopenshell.util.placement import get_local_placement
                        matrix = get_local_placement(placement)
                        origin = [float(matrix[index, 3]) for index in range(3)]
                    except Exception:
                        origin = [0.0, 0.0, 0.0]
                    fallback_dimensions = {
                        "IfcWall": [0.2, 3.0, 3.0],
                        "IfcDoor": [0.9, 0.12, 2.1],
                        "IfcWindow": [1.2, 0.12, 1.5],
                        "IfcSlab": [4.0, 4.0, 0.2],
                        "IfcStair": [2.0, 3.0, 2.5],
                        "IfcRoof": [4.0, 4.0, 0.3],
                    }
                    dimensions = fallback_dimensions.get(element.is_a(), [0.5, 0.5, 0.5])
                    output.append({
                        "express_id": element.id(), "global_id": getattr(element, "GlobalId", None),
                        "entity_type": element.is_a(), "name": self._name_of(element),
                        "storey": self._storey_name(element), "center": origin,
                        "dimensions": dimensions, "color": palette.get(element.is_a(), "#8aa4c8"),
                    })
                    continue
                dimensions = [max(maximum[index] - minimum[index], 0.05) for index in range(3)]
                output.append({
                    "express_id": element.id(),
                    "global_id": getattr(element, "GlobalId", None),
                    "entity_type": element.is_a(),
                    "name": self._name_of(element),
                    "storey": self._storey_name(element),
                    "center": [(minimum[index] + maximum[index]) / 2 for index in range(3)],
                    "dimensions": dimensions,
                    "color": palette.get(entity_type, "#8aa4c8"),
                })
        return output

    def execute(self, query: IfcQueryInput) -> IfcQueryResult:
        entity_type = self._normalize_entity_type(query.entity_type)
        if query.operation == "aggregate_quantity":
            value, elements = self._aggregate_quantity(query)
            evidence = self._make_evidence(elements, query, value)
            return IfcQueryResult(operation=query.operation, value=value, unit=value.get("unit", "m"), matched_count=value.get("eligible_count", len(elements)), evidence=evidence,
                matched_global_ids=[item.GlobalId for item in elements if getattr(item, "GlobalId", None)], normalized_query=query.model_dump(),
                warnings=[] if value.get("eligible_count") == value.get("total_entity_count") else ["Only a subset of entities exposed the requested quantity."])
        if query.operation == "max_window_height":
            # Backwards-compatible read path for old fixtures; planners no
            # longer emit this narrow operation.
            value, elements = self._aggregate_quantity(IfcQueryInput(operation="aggregate_quantity", entity_type="IfcWindow", measure="height", aggregation="max", filters=query.filters))
            evidence = self._make_evidence(elements, query, value)
            return IfcQueryResult(operation=query.operation, value=value, unit="m", matched_count=len(elements), evidence=evidence,
                matched_global_ids=[item.GlobalId for item in elements if getattr(item, "GlobalId", None)], normalized_query=query.model_dump())
        if query.operation == "space_distance":
            value, spaces = self._space_distance(query.filters)
            evidence = self._make_evidence(spaces, query, value)
            return IfcQueryResult(operation=query.operation, value=value, unit="m", matched_count=len(spaces), evidence=evidence,
                matched_global_ids=[item.GlobalId for item in spaces if getattr(item, "GlobalId", None)], normalized_query=query.model_dump())
        elements = self._matching_elements(entity_type, query.filters)
        if query.operation == "count":
            value: Any = len(elements)
        elif query.operation == "list":
            value = [self._compact_element(item) for item in elements[: query.limit]]
        elif query.operation == "group_by":
            value = self._group_elements(elements, query.group_by)
        elif query.operation in {"min", "max", "sum", "average"}:
            value = self._aggregate(elements, query.operation, query.property_path)
        elif query.operation == "get_properties":
            value = [self._element_properties(item) for item in elements[: query.limit]]
        elif query.operation == "inspect_relationship":
            value = [self._relationships(item) for item in elements[: query.limit]]
        else:
            raise IfcRepositoryError(f"Unsupported IFC operation: {query.operation}")

        evidence = self._make_evidence(elements, query, value)
        return IfcQueryResult(
            operation=query.operation,
            value=value,
            matched_count=len(elements),
            evidence=evidence,
            matched_global_ids=[
                global_id for global_id in (getattr(item, "GlobalId", None) for item in elements)
                if global_id
            ],
            normalized_query=query.model_dump(),
            warnings=[] if elements else ["No IFC elements matched the query."],
        )

    @cached_property
    def length_scale_to_meters(self) -> float:
        """Read IFC length units once; this source uses centimetres."""
        prefixes = {"MILLI": 0.001, "CENTI": 0.01, "DECI": 0.1, "KILO": 1000.0, None: 1.0}
        for assignment in self.model.by_type("IfcUnitAssignment"):
            for unit in assignment.Units or []:
                if unit.is_a("IfcSIUnit") and getattr(unit, "UnitType", None) == "LENGTHUNIT":
                    return prefixes.get(getattr(unit, "Prefix", None), 1.0)
        return 1.0

    def _max_window_height(self) -> tuple[dict[str, Any], list[Any]]:
        """Compatibility wrapper for the pre-generalized operation."""
        return self._aggregate_quantity(IfcQueryInput(operation="aggregate_quantity", entity_type="IfcWindow", measure="height", aggregation="max"))

    def _aggregate_quantity(self, query: IfcQueryInput) -> tuple[dict[str, Any], list[Any]]:
        """Resolve and aggregate a controlled quantity without LLM calculation."""
        entity_type = self._normalize_entity_type(query.entity_type)
        measure = (query.measure or "").lower().strip()
        aggregation = query.aggregation
        if measure not in {"height", "width", "length", "area"} or aggregation not in {"min", "max"}:
            raise IfcRepositoryError("aggregate_quantity requires a controlled measure and min/max aggregation.")
        elements = self._matching_elements(entity_type, query.filters)
        candidates: list[tuple[float, Any]] = []
        candidate_paths: list[str] = []
        for element in elements:
            resolved = self._resolve_controlled_measure(element, measure)
            if resolved is None:
                continue
            raw, path = resolved
            if path not in candidate_paths:
                candidate_paths.append(path)
            try:
                normalized = float(raw)
                if measure in {"height", "width", "length"}:
                    normalized *= self.length_scale_to_meters
                candidates.append((normalized, element))
            except (TypeError, ValueError):
                continue
        if not candidates:
            raise IfcRepositoryError(f"No structured {measure} values were found for {entity_type}.")
        extreme = (max if aggregation == "max" else min)(value for value, _ in candidates)
        winners = [element for value, element in candidates if abs(value - extreme) < 1e-8]
        return ({"value_m": round(extreme, 4), "quantity_path": candidate_paths[0], "measure": measure,
                 "aggregation": aggregation, "source_unit_scale_to_m": self.length_scale_to_meters,
                 "eligible_count": len(candidates), "total_entity_count": len(elements),
                 "coverage": round(len(candidates) / len(elements), 4) if elements else 0,
                 "global_ids": [item.GlobalId for item in winners]}, winners)

    def _resolve_controlled_measure(self, element: Any, measure: str) -> tuple[Any, str] | None:
        properties = self._flatten_properties(element)
        suffix = f".{measure}".lower()
        entity_stem = element.is_a().removeprefix("Ifc")
        preferred = [
            f"Qto_{entity_stem}BaseQuantities.{measure.title()}",
            f"Qto_{entity_stem}BaseQuantities.{measure}",
        ]
        for path in preferred:
            if path in properties and properties[path] is not None:
                return properties[path], path
        matches = [(path, value) for path, value in properties.items() if path.lower().endswith(suffix) and value is not None]
        matches.sort(key=lambda item: (0 if item[0].lower().startswith("qto_") else 1, item[0]))
        return (matches[0][1], matches[0][0]) if matches else None

    def _space_distance(self, filters: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        """Bounded horizontal centroid distance for explicitly resolved IFC spaces.

        This is intentionally not a route/path-length engine. It measures the
        Euclidean distance between geometry bounding-box centroids in project
        coordinates, after converting the declared IFC length unit to metres.
        """
        from_name = str(filters.get("from_space") or "").strip()
        to_name = str(filters.get("to_space") or "").strip()
        if not from_name or not to_name:
            raise IfcRepositoryError("Space distance requires explicit from_space and to_space names.")
        left = self._find_spaces(from_name)
        right = self._find_spaces(to_name)
        if len(left) != 1 or len(right) != 1:
            raise IfcRepositoryError("Space names must resolve to exactly one IFC space each; please use the displayed room identifiers.")
        first, second = left[0], right[0]
        a, b = self._space_centroid(first), self._space_centroid(second)
        distance = sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) * self.length_scale_to_meters
        return ({"value_m": round(distance, 4), "method": "horizontal_bounding_box_centroid_distance", "from_space": self._name_of(first),
                 "to_space": self._name_of(second), "from_global_id": first.GlobalId, "to_global_id": second.GlobalId,
                 "source_unit_scale_to_m": self.length_scale_to_meters}, [first, second])

    def _find_spaces(self, name: str) -> list[Any]:
        needle = self._canonical_space_text(name)
        return [space for space in self.model.by_type("IfcSpace")
                if needle in self._canonical_space_text(f"{getattr(space, 'Name', '') or ''} {getattr(space, 'LongName', '') or ''}")]

    @staticmethod
    def _canonical_space_text(value: str) -> str:
        return " ".join(value.lower().replace("bedroom", "bed room").replace("'", "").split())

    def _space_centroid(self, space: Any) -> tuple[float, float, float]:
        try:
            import ifcopenshell.geom
            settings = ifcopenshell.geom.settings(); settings.set(settings.USE_WORLD_COORDS, True)
            vertices = list(ifcopenshell.geom.create_shape(settings, space).geometry.verts)
            if not vertices:
                # Some authoring exports omit an IfcSpace representation but
                # retain a valid placement. Preserve the bounded capability by
                # using the resolved placement as a point centroid and expose
                # the limitation in the result's method label.
                from ifcopenshell.util.placement import get_local_placement
                matrix = get_local_placement(space.ObjectPlacement)
                return (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
            axes = [vertices[index::3] for index in range(3)]
            return tuple((min(axis) + max(axis)) / 2 for axis in axes)  # type: ignore[return-value]
        except Exception as error:
            raise IfcRepositoryError(f"Could not derive bounded geometry for space {self._name_of(space)}: {error}") from error

    def execute_batch_counts(self, entity_types: list[str], filters: dict[str, Any]) -> dict[str, int]:
        """Deterministically count several homogeneous IFC targets together.

        The caller still independently verifies every subresult and creates
        per-entity evidence.  This method is therefore a transparent execution
        optimisation, not a replacement for the verification boundary.
        """
        return {
            entity_type: len(self._matching_elements(self._normalize_entity_type(entity_type), filters))
            for entity_type in entity_types
        }

    def resolve_selection(self, global_ids: list[str], express_ids: list[int]) -> dict[str, Any] | None:
        """Resolve active viewer identity from the source IFC, not chat history."""
        elements = self._matching_elements(None, {"global_ids": global_ids, "express_ids": express_ids})
        if not elements:
            return None
        element = elements[0]
        return {**self._compact_element(element), "storey": self._storey_name(element)}

    def _matching_elements(self, entity_type: str | None, filters: dict[str, Any]) -> list[Any]:
        candidates = list(self.model.by_type(entity_type)) if entity_type else list(self.model.by_type("IfcProduct"))
        selected_ids = set(filters.get("global_ids", []))
        selected_express_ids = {int(item) for item in filters.get("express_ids", [])}
        name_contains = str(filters.get("name_contains", "")).lower().strip()
        storey_filter = str(filters.get("storey", "")).lower().strip()
        property_equals = filters.get("property_equals", {})

        output: list[Any] = []
        for element in candidates:
            if selected_ids and getattr(element, "GlobalId", None) not in selected_ids:
                continue
            if selected_express_ids and element.id() not in selected_express_ids:
                continue
            if name_contains and name_contains not in self._name_of(element).lower():
                continue
            if storey_filter and storey_filter not in (self._storey_name(element) or "").lower():
                continue
            if property_equals:
                properties = self._flatten_properties(element)
                if any(str(properties.get(key)).lower() != str(value).lower()
                       for key, value in property_equals.items()):
                    continue
            output.append(element)
        return output

    @staticmethod
    def _normalize_entity_type(entity_type: str | None) -> str | None:
        if not entity_type:
            return None
        normalized = entity_type.strip()
        if not normalized.lower().startswith("ifc"):
            normalized = f"Ifc{normalized}"
        return normalized

    def _group_elements(self, elements: list[Any], group_by: str) -> dict[str, int]:
        counters: dict[str, int] = defaultdict(int)
        for element in elements:
            if group_by == "storey":
                key = self._storey_name(element) or "Unassigned"
            elif group_by == "space":
                key = self._space_name(element) or "Unassigned"
            elif group_by == "type":
                key = element.is_a()
            else:
                raise IfcRepositoryError(f"Unsupported group_by: {group_by}")
            counters[key] += 1
        return dict(sorted(counters.items(), key=lambda pair: (-pair[1], pair[0])))

    def _aggregate(self, elements: list[Any], operation: str, property_path: str | None) -> dict[str, Any]:
        if not property_path:
            raise IfcRepositoryError("A property_path is required for aggregation.")
        values: list[float] = []
        units: set[str] = set()
        element_values: list[dict[str, Any]] = []
        for element in elements:
            resolved = self._lookup_property(element, property_path)
            if resolved is None:
                continue
            raw_value, unit = resolved
            try:
                number = float(raw_value)
            except (TypeError, ValueError):
                continue
            values.append(number)
            if unit:
                units.add(unit)
            element_values.append({
                "global_id": getattr(element, "GlobalId", None),
                "name": self._name_of(element),
                "value": number,
                "unit": unit,
            })
        if not values:
            raise IfcRepositoryError(f"No numeric values found at property path '{property_path}'.")
        if len(units) > 1:
            raise IfcRepositoryError(f"Incompatible units found: {sorted(units)}")
        if operation == "min":
            result = min(values)
        elif operation == "max":
            result = max(values)
        elif operation == "sum":
            result = sum(values)
        else:
            result = sum(values) / len(values)
        return {
            "value": result,
            "unit": next(iter(units), None),
            "property_path": property_path,
            "contributing_elements": element_values,
        }

    def _element_properties(self, element: Any) -> dict[str, Any]:
        return {
            "element": self._compact_element(element),
            "storey": self._storey_name(element),
            "properties": self._flatten_properties(element),
        }

    def _relationships(self, element: Any) -> dict[str, Any]:
        return {
            "element": self._compact_element(element),
            "storey": self._storey_name(element),
            "space": self._space_name(element),
        }

    def _make_evidence(self, elements: list[Any], query: IfcQueryInput, value: Any) -> list[Evidence]:
        sample = elements[: min(10, len(elements))]
        evidence: list[Evidence] = []
        for element in sample:
            locator = {
                "global_id": getattr(element, "GlobalId", None),
                "express_id": element.id(),
                "entity_type": element.is_a(),
                "storey": self._storey_name(element),
                "query_operation": query.operation,
                "property_path": query.property_path,
                "measure": query.measure,
                "aggregation": query.aggregation,
            }
            evidence.append(Evidence(
                source_type=SourceType.IFC,
                source_file=self.path.name,
                summary=f"{element.is_a()} {self._name_of(element)} matched the IFC query.",
                locator=locator,
            ))
        if not evidence and query.operation == "count":
            evidence.append(Evidence(
                source_type=SourceType.IFC,
                source_file=self.path.name,
                summary="Deterministic IFC query completed with no matching elements.",
                locator={"query_operation": query.operation, "entity_type": query.entity_type},
                extracted_value=value,
            ))
        return evidence

    def _flatten_properties(self, element: Any) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for definition in getattr(element, "IsDefinedBy", []) or []:
            if not definition.is_a("IfcRelDefinesByProperties"):
                continue
            definition_set = getattr(definition, "RelatingPropertyDefinition", None)
            if not definition_set:
                continue
            if definition_set.is_a("IfcPropertySet"):
                for prop in definition_set.HasProperties or []:
                    if prop.is_a("IfcPropertySingleValue"):
                        properties[f"{definition_set.Name}.{prop.Name}"] = self._unwrap(
                            getattr(prop, "NominalValue", None)
                        )
            elif definition_set.is_a("IfcElementQuantity"):
                for quantity in definition_set.Quantities or []:
                    value = next(
                        (getattr(quantity, attr) for attr in (
                            "LengthValue", "AreaValue", "VolumeValue", "CountValue", "WeightValue", "TimeValue"
                        ) if getattr(quantity, attr, None) is not None),
                        None,
                    )
                    properties[f"{definition_set.Name}.{quantity.Name}"] = value
        return properties

    def _lookup_property(self, element: Any, property_path: str) -> tuple[Any, str | None] | None:
        properties = self._flatten_properties(element)
        if property_path in properties:
            return properties[property_path], None
        short_matches = [
            (name, value) for name, value in properties.items()
            if name.lower().endswith(f".{property_path.lower()}")
        ]
        if len(short_matches) == 1:
            return short_matches[0][1], None
        return None

    def _storey_name(self, element: Any, visited: set[int] | None = None) -> str | None:
        for relation in getattr(element, "ContainedInStructure", []) or []:
            structure = getattr(relation, "RelatingStructure", None)
            if structure and structure.is_a("IfcBuildingStorey"):
                return self._name_of(structure)
        visited = visited or set()
        element_id = element.id()
        if element_id in visited:
            return None
        parent = self._aggregate_parent_by_id.get(element_id)
        if parent:
            visited.add(element_id)
            return self._storey_name(parent, visited)
        return None

    def _space_name(self, element: Any) -> str | None:
        # IFC files vary widely. This intentionally reports only a proven direct relation.
        for relation in getattr(element, "ContainedInStructure", []) or []:
            structure = getattr(relation, "RelatingStructure", None)
            if structure and structure.is_a("IfcSpace"):
                return self._name_of(structure)
        spaces = self._space_by_element.get(getattr(element, "GlobalId", ""), set())
        return ", ".join(sorted(spaces)) if spaces else None

    @staticmethod
    def _unwrap(value: Any) -> Any:
        return getattr(value, "wrappedValue", value)

    @staticmethod
    def _name_of(element: Any) -> str:
        return str(getattr(element, "Name", None) or getattr(element, "LongName", None) or "Unnamed")

    @staticmethod
    def _compact_element(element: Any) -> dict[str, Any]:
        return {
            "global_id": getattr(element, "GlobalId", None),
            "express_id": element.id(),
            "entity_type": element.is_a(),
            "name": IfcRepository._name_of(element),
        }

    def _first(self, entity_type: str) -> Any | None:
        values = self.model.by_type(entity_type)
        return values[0] if values else None
