from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.schemas.models import DocumentQueryInput, DocumentQueryResult, Evidence, SourceType
from app.schemas.vision import (
    VisionBoardLocalization,
    VisionCandidateVerification,
    VisionEvidenceVerification,
    VisionFieldCandidates,
    VisionFieldExtraction,
)

# Tolerances for reconstructing table structure from word-level coordinates
# (SPEC-M2P1 §4.1). These characterize *this* fixture's layout -- clean,
# left-aligned columns with wide inter-column gaps -- not a general
# table-extraction algorithm (SPEC-M2P1 §3 generality caveat).
_ROW_Y_TOLERANCE_PT = 4.0
_COLUMN_GAP_THRESHOLD_PT = 20.0


@dataclass
class _TableCell:
    text: str
    bbox: list[float]


@dataclass
class _TableColumn:
    label: str
    bbox: list[float]


@dataclass
class _Table:
    columns: list[_TableColumn]
    rows: list[list["_TableCell | None"]]


class DocumentAnalyzer:
    """MVP document adapter: native text/layout first, vision adapter second."""

    def __init__(self, pdf_path: Path, evidence_dir: Path) -> None:
        self.pdf_path = pdf_path
        self.evidence_dir = evidence_dir

    @property
    def available(self) -> bool:
        return self.pdf_path.exists()

    def inspect(self) -> dict:
        if not self.available:
            return {"available": False, "path": str(self.pdf_path)}
        try:
            import fitz
        except ImportError as error:
            raise RuntimeError("PyMuPDF is not installed") from error
        document = fitz.open(self.pdf_path)
        return {
            "available": True,
            "page_count": len(document),
            "page_sizes": [[page.rect.width, page.rect.height] for page in document],
            "native_text_preview": document[0].get_text()[:1000] if document else "",
        }

    def render_page(self, page_number: int = 1, scale: float = 2.0) -> Path:
        if not self.available:
            raise FileNotFoundError(self.pdf_path)
        import fitz
        document = fitz.open(self.pdf_path)
        page = document[page_number - 1]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        output = self.evidence_dir / f"pdf-page-{page_number}-{uuid4().hex}.png"
        pixmap.save(output)
        return output

    def crop_evidence(self, page_number: int, bbox: list[float] | None) -> Path | None:
        """Persist the cited evidence crop when a visual extractor supplied one."""
        if not bbox or len(bbox) != 4:
            return None
        import fitz
        document = fitz.open(self.pdf_path)
        page = document[page_number - 1]
        rect = fitz.Rect(*bbox) & page.rect
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            return None
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
        output = self.evidence_dir / f"pdf-crop-{page_number}-{uuid4().hex}.png"
        pixmap.save(output)
        return output

    def page_bbox(self, page_number: int) -> list[float]:
        import fitz
        document = fitz.open(self.pdf_path)
        rect = document[page_number - 1].rect
        return [rect.x0, rect.y0, rect.x1, rect.y1]

    def _read_table(self, page_number: int) -> "_Table | None":
        """Reconstruct rows and columns from word-level coordinates (SPEC-M2P1 §4.1).

        Words are clustered into rows by ``y`` and into column bands, derived
        from the header row's ``x`` extents, by ``x`` -- both within a
        tolerance, not exact coordinate equality. The header row is
        identified as the first row that resolves to more than one column
        band (a title or subtitle line above it is prose, not a table row,
        and clusters as a single band); data rows are then taken while the
        row-to-row vertical pitch stays consistent with the rows already
        accepted, which stops at a following prose section (for example
        "Notes") without hardcoding its content. This characterizes this
        fixture's clean, left-aligned layout; it is not a general
        table-extraction algorithm (SPEC-M2P1 §3 generality caveat).
        """
        if not self.available:
            return None
        import fitz
        document = fitz.open(self.pdf_path)
        page = document[page_number - 1]
        words = page.get_text("words")
        if not words:
            return None
        rows = self._cluster_rows(words)
        header_index = next((i for i, row in enumerate(rows) if len(self._cluster_header_columns(row)) >= 2), None)
        if header_index is None:
            return None
        columns = self._cluster_header_columns(rows[header_index])
        data_rows = self._select_data_rows(rows, header_index)
        table_rows = [self._assign_row_to_columns(row, columns) for row in data_rows]
        return _Table(columns=columns, rows=table_rows)

    @staticmethod
    def _cluster_rows(words: list[tuple]) -> list[list[tuple]]:
        ordered = sorted(words, key=lambda w: (w[1], w[0]))
        rows: list[list[tuple]] = []
        anchor_y: float | None = None
        for word in ordered:
            y0 = word[1]
            if not rows or anchor_y is None or abs(y0 - anchor_y) > _ROW_Y_TOLERANCE_PT:
                rows.append([])
                anchor_y = y0
            rows[-1].append(word)
        return [sorted(row, key=lambda w: w[0]) for row in rows]

    @staticmethod
    def _row_anchor(row: list[tuple]) -> float:
        return min(word[1] for word in row)

    @classmethod
    def _select_data_rows(cls, rows: list[list[tuple]], header_index: int) -> list[list[tuple]]:
        """Take rows below the header while the row-to-row pitch stays regular.

        A table's rows repeat at a near-constant vertical spacing; a
        following prose section (title, notes) breaks that pitch. Stopping
        on a pitch outlier bounds the table without hardcoding a row count
        or any section heading text.
        """
        kept: list[list[tuple]] = []
        reference_pitch: float | None = None
        prev_y = cls._row_anchor(rows[header_index])
        for row in rows[header_index + 1:]:
            y = cls._row_anchor(row)
            gap = y - prev_y
            if reference_pitch is not None and gap > reference_pitch * 1.5:
                break
            kept.append(row)
            reference_pitch = gap if reference_pitch is None else (reference_pitch + gap) / 2
            prev_y = y
        return kept

    @staticmethod
    def _cluster_header_columns(header_words: list[tuple]) -> list[_TableColumn]:
        ordered = sorted(header_words, key=lambda w: w[0])
        groups: list[list[tuple]] = []
        prev_x1: float | None = None
        for word in ordered:
            x0 = word[0]
            if prev_x1 is None or (x0 - prev_x1) > _COLUMN_GAP_THRESHOLD_PT:
                groups.append([])
            groups[-1].append(word)
            prev_x1 = word[2]
        columns = []
        for group in groups:
            label = " ".join(w[4] for w in group)
            columns.append(_TableColumn(
                label=label,
                bbox=[min(w[0] for w in group), min(w[1] for w in group), max(w[2] for w in group), max(w[3] for w in group)],
            ))
        return columns

    @staticmethod
    def _assign_row_to_columns(row_words: list[tuple], columns: list[_TableColumn]) -> list["_TableCell | None"]:
        boundaries = []
        for i, column in enumerate(columns):
            left = -math.inf if i == 0 else (columns[i - 1].bbox[2] + column.bbox[0]) / 2
            right = math.inf if i == len(columns) - 1 else (column.bbox[2] + columns[i + 1].bbox[0]) / 2
            boundaries.append((left, right))
        buckets: list[list[tuple]] = [[] for _ in columns]
        for word in row_words:
            center_x = (word[0] + word[2]) / 2
            for i, (left, right) in enumerate(boundaries):
                if left <= center_x < right:
                    buckets[i].append(word)
                    break
        cells: list[_TableCell | None] = []
        for bucket in buckets:
            if not bucket:
                cells.append(None)
                continue
            bucket = sorted(bucket, key=lambda w: w[0])
            cells.append(_TableCell(
                text=" ".join(w[4] for w in bucket),
                bbox=[min(w[0] for w in bucket), min(w[1] for w in bucket), max(w[2] for w in bucket), max(w[3] for w in bucket)],
            ))
        return cells

    @staticmethod
    def _record_candidates(table: _Table) -> list[tuple[int, str]]:
        """(row_index, identifier) for every row whose first column has text (OD-9b)."""
        candidates = []
        for index, row in enumerate(table.rows):
            cell = row[0] if row else None
            if cell and cell.text.strip():
                candidates.append((index, cell.text.strip()))
        return candidates

    @staticmethod
    def _match_records(question: str, candidates: list[tuple[int, str]]) -> list[int]:
        lowered_question = question.lower()
        return [index for index, identifier in candidates if identifier.lower() in lowered_question]

    def _match_columns(self, requested_field: str | None, columns: list[_TableColumn]) -> list[int]:
        requested_canonical = self.canonical_field(requested_field)
        if not requested_canonical:
            return []
        return [index for index, column in enumerate(columns) if requested_canonical in self.canonical_field(column.label)]

    def native_lookup(self, query: DocumentQueryInput) -> DocumentQueryResult:
        """Row- and column-aware deterministic extraction (SPEC-M2P1 §4.1/§4.2).

        Locates the row whose first-column identifier matches the question
        and the column whose header matches the requested field, and returns
        that cell's value and real bbox. Confidence reflects what was
        actually established (OD-11): unambiguous -> 0.95; ambiguous
        (0 or >1 candidate rows, or >1 candidate columns) -> 0.4; the
        requested field absent from the document entirely -> 0.0. No value
        is ever returned that was not read from a uniquely resolved cell.
        """
        if not self.available:
            raise FileNotFoundError(self.pdf_path)
        page_number = query.page_hint or 1
        table = self._read_table(page_number)
        if table is None:
            return DocumentQueryResult(
                value=None, unit=None, page=page_number, bbox=None, extraction_method="native_text",
                confidence=0.0, evidence=[],
                ambiguity="The document's table structure could not be read natively.",
            )

        record_candidates = self._record_candidates(table)
        record_lookup = dict(record_candidates)
        record_matches = self._match_records(query.question, record_candidates)
        column_matches = self._match_columns(query.field, table.columns)

        if not column_matches:
            available = ", ".join(column.label for column in table.columns)
            return DocumentQueryResult(
                value=None, unit=None, page=page_number, bbox=None, extraction_method="native_text",
                confidence=0.0, evidence=[],
                ambiguity=f"The document does not contain a '{query.field}' field. Available fields: {available}.",
            )

        if len(column_matches) == 1 and len(record_matches) == 1:
            column_index = column_matches[0]
            row_index = record_matches[0]
            column = table.columns[column_index]
            cell = table.rows[row_index][column_index]
            identifier = record_lookup[row_index]
            if cell is None or not cell.text.strip():
                return DocumentQueryResult(
                    value=None, unit=None, page=page_number, bbox=None, extraction_method="native_text",
                    confidence=0.0, evidence=[],
                    ambiguity=f"No value was found for {column.label} on {identifier}.",
                )
            crop = self.crop_evidence(page_number, cell.bbox)
            evidence = [Evidence(
                source_type=SourceType.PDF,
                source_file=self.pdf_path.name,
                summary=f"Native table cell: {identifier} · {column.label} = {cell.text}.",
                locator={
                    "page": page_number,
                    "bbox": cell.bbox,
                    "field": query.field,
                    "extraction_method": "native_text",
                    "record": identifier,
                    "column": column.label,
                    "evidence_crop": crop.name if crop else None,
                },
                extracted_value=cell.text,
                confidence=0.95,
            )]
            return DocumentQueryResult(
                value=cell.text, unit=None, page=page_number, bbox=cell.bbox,
                extraction_method="native_text", confidence=0.95, evidence=evidence, ambiguity=None,
            )

        if len(column_matches) > 1:
            matched_labels = ", ".join(table.columns[i].label for i in column_matches)
            ambiguity = f"Multiple fields match '{query.field}': {matched_labels}. Please specify one."
        elif not record_matches:
            column_label = table.columns[column_matches[0]].label
            available_records = ", ".join(identifier for _, identifier in record_candidates)
            ambiguity = f"I could not identify a specific record for {column_label} in your question. Please specify one of: {available_records}."
        else:
            column_label = table.columns[column_matches[0]].label
            matched_records = ", ".join(record_lookup[i] for i in record_matches)
            ambiguity = f"Your question matches multiple records ({matched_records}) for {column_label}; please specify one."
        return DocumentQueryResult(
            value=None, unit=None, page=page_number, bbox=None, extraction_method="native_text",
            confidence=0.4, evidence=[], ambiguity=ambiguity,
        )

    @staticmethod
    def target_board(question: str) -> str | None:
        match = re.search(r"\b(?:SMDB|DB)-[A-Z0-9]+(?:-[A-Z0-9]+)+\b", question.upper())
        return match.group(0) if match else None

    @staticmethod
    def target_field(question: str) -> str | None:
        lowered = question.lower()
        if "diversity factor" in lowered or "diversity factor for" in lowered:
            return "Diversity Factor"
        if "total connected load" in lowered or "connected load" in lowered:
            return "Connected Load"
        if "after diversity" in lowered or "diversity load" in lowered:
            return "After Diversity Load"
        return None

    @staticmethod
    def canonical_field(value: str | None) -> str:
        """Compare drawing labels without rejecting harmless unit suffixes."""
        lowered = (value or "").lower()
        lowered = lowered.replace("in kw", "").replace("(kw)", "")
        return re.sub(r"[^a-z0-9]+", " ", lowered).strip()

    async def localize_board(self, provider, query: DocumentQueryInput, board: str) -> tuple[VisionBoardLocalization, Path]:
        page_image = self.render_page(query.page_hint or 1)
        location = await provider.vision_structured(
            purpose="pdf_board_localize",
            image_base64=self.image_as_base64(page_image),
            response_model=VisionBoardLocalization,
            prompt=(
                "Locate exactly one distribution-board schedule section in this engineering drawing. "
                f"Target board: {board}. Return a bounding box [x0,y0,x1,y1] in original PDF page "
                "coordinates enclosing the table and its totals. Do not extract the final value yet. "
                "If the target board cannot be uniquely located, state ambiguity."
            ),
        )
        return location, page_image

    async def board_candidates(
        self, provider, query: DocumentQueryInput, board: str, board_bbox: list[float]
    ) -> tuple[VisionFieldCandidates, Path]:
        crop = self.crop_evidence(query.page_hint or 1, board_bbox)
        if not crop:
            raise ValueError("The board-localization bounding box could not be cropped safely.")
        field = self.target_field(query.question) or query.field
        candidates = await provider.vision_structured(
            purpose="pdf_board_extract",
            image_base64=self.image_as_base64(crop),
            response_model=VisionFieldCandidates,
            prompt=(
                "Inspect only this already-localized distribution-board table crop. Return candidates for "
                f"board {board} and field {field}. Each candidate must include its board, exact field label, "
                "value, unit, and crop-relative bbox if visible. Never choose a value from another board. "
                "If the crop does not make a unique field/value pair visible, set ambiguity."
            ),
        )
        return candidates, crop

    async def verify_board_candidate(
        self, provider, query: DocumentQueryInput, board: str, candidate, crop: Path
    ) -> VisionCandidateVerification:
        return await provider.vision_structured(
            purpose="pdf_board_verify",
            image_base64=self.image_as_base64(crop),
            response_model=VisionCandidateVerification,
            prompt=(
                "Independently verify this claimed extraction against the supplied single-board table crop. "
                "Set supported=true only when board, field, value, and uniqueness all visibly match. "
                f"Target board: {board}\nTarget field: {self.target_field(query.question) or query.field}\n"
                f"Candidate: {candidate.model_dump()}"
            ),
        )

    async def vision_lookup(self, provider, query: DocumentQueryInput) -> DocumentQueryResult:
        """Extract a target field from a rendered drawing and retain its visual evidence."""
        page_number = query.page_hint or 1
        page_image = self.render_page(page_number)
        extraction = await provider.vision_structured(
            purpose="pdf_extract",
            image_base64=self.image_as_base64(page_image),
            response_model=VisionFieldExtraction,
            prompt=(
                "You are extracting a field from a construction electrical drawing. "
                "Return a value only when the exact field/row/column is visibly supported. "
                f"Question: {query.question}\nRequested field: {query.field}\n"
                "Use page coordinates in the original PDF coordinate system when possible. "
                "State ambiguity instead of guessing."
            ),
        )
        bbox = extraction.bbox or self.page_bbox(extraction.page)
        crop = self.crop_evidence(extraction.page, bbox)
        evidence = [Evidence(
            source_type=SourceType.PDF,
            source_file=self.pdf_path.name,
            summary=f"Vision extraction for field: {query.field}",
            locator={
                "page": extraction.page,
                "bbox": bbox,
                "field": query.field,
                "extraction_method": "vision",
                "rendered_image": page_image.name,
                "evidence_crop": crop.name if crop else None,
            },
            extracted_value=extraction.value,
            confidence=extraction.confidence,
        )]
        return DocumentQueryResult(
            value=extraction.value,
            unit=extraction.unit,
            page=extraction.page,
            bbox=bbox,
            extraction_method="vision",
            confidence=extraction.confidence,
            evidence=evidence,
            ambiguity=extraction.ambiguity,
        )

    async def verify_vision_extraction(self, provider, query: DocumentQueryInput, result: DocumentQueryResult) -> VisionEvidenceVerification:
        """Use a separate visual pass to validate a candidate against its source page.

        This deliberately does not reuse the extraction response: the verifier is
        asked only whether the claimed value is visibly supported and whether the
        cited page region is appropriate. A production version would use a crop
        generated from the returned bbox; the scoped MVP retains the rendered page
        and full source evidence when the model cannot provide reliable coordinates.
        """
        image_path = self.render_page(result.page or 1)
        return await provider.vision_structured(
            purpose="pdf_verify",
            image_base64=self.image_as_base64(image_path),
            response_model=VisionEvidenceVerification,
            prompt=(
                "You are independently verifying a construction drawing extraction. "
                "Do not infer or improve the candidate. Mark supported=true only if the "
                "exact value and its field are visibly present on the supplied drawing and "
                "the question is not ambiguous among multiple totals.\n"
                f"Question: {query.question}\nCandidate value: {result.value}\n"
                f"Candidate field: {query.field}\nCandidate page: {result.page}\n"
                f"Candidate region: {result.bbox}\n"
                "If multiple plausible totals exist, return supported=false and explain what clarification is needed."
            ),
        )

    @staticmethod
    def image_as_base64(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode("ascii")
