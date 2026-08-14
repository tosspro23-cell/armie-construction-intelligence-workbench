from __future__ import annotations

import base64
import re
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

    def native_lookup(self, query: DocumentQueryInput) -> DocumentQueryResult:
        """Best-effort native text result. Complex engineering tables must use vision."""
        if not self.available:
            raise FileNotFoundError(self.pdf_path)
        import fitz
        document = fitz.open(self.pdf_path)
        page_number = query.page_hint or 1
        page = document[page_number - 1]
        text = page.get_text("text")
        needle = query.field.lower()
        match = next((line for line in text.splitlines() if needle in line.lower()), None)
        confidence = 0.55 if match else 0.0
        evidence = []
        if match:
            evidence = [Evidence(
                source_type=SourceType.PDF,
                source_file=self.pdf_path.name,
                summary=f"Native PDF text line matching '{query.field}'.",
                locator={
                    "page": page_number,
                    "bbox": None,
                    "field": query.field,
                    "extraction_method": "native_text",
                },
                extracted_value=match,
                confidence=confidence,
            )]
        return DocumentQueryResult(
            value=match,
            unit=None,
            page=page_number,
            bbox=None,
            extraction_method="native_text",
            confidence=confidence,
            evidence=evidence,
            ambiguity=None if match else "No reliable native-text match; vision analysis is required.",
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
            return "Total Connected Load"
        if "after diversity" in lowered or "diversity load" in lowered:
            return "After Diversity Load"
        return None

    def ambiguity_policy(self, query: DocumentQueryInput) -> str | None:
        """Deterministic safety gate for labels repeated across the supplied schedule."""
        field = self.target_field(query.question)
        board = self.target_board(query.question)
        if field == "Total Connected Load" and not board:
            return (
                "The drawing contains multiple Total Connected Load values. Please specify the "
                "distribution board or schedule section, for example DB-L1-A."
            )
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
