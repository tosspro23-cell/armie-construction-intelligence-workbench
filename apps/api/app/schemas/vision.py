from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class VisionFieldExtraction(BaseModel):
    value: str | float | int | None = None
    unit: str | None = None
    page: int = Field(ge=1)
    bbox: list[float] | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: str
    ambiguity: str | None = None

    @field_validator("ambiguity", mode="before")
    @classmethod
    def normalise_no_ambiguity(cls, value):
        return None if isinstance(value, str) and value.strip().lower() in {"none", "null", "n/a", "no"} else value

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value


class VisionEvidenceVerification(BaseModel):
    supported: bool
    confidence: float = Field(ge=0, le=1)
    rationale: str

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value


class VisionViewerInspection(BaseModel):
    target: str | None = None
    target_visible: bool = False
    observation: str | None = None
    classification: str | None = None
    reason: str | None = None
    claim: str = ""
    confidence: float = Field(ge=0, le=1)
    sufficient_view: bool
    occlusion_detected: bool
    limitations: list[str] = Field(default_factory=list)
    relevant_selected_global_ids: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value


class VisionBoardLocalization(BaseModel):
    board: str | None = None
    bbox: list[float] | None = None
    confidence: float = Field(ge=0, le=1)
    ambiguity: str | None = None
    rationale: str

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value

    @field_validator("ambiguity", mode="before")
    @classmethod
    def normalise_no_ambiguity(cls, value):
        if not isinstance(value, str):
            return value
        return None if value.strip().lower().startswith(("no ambiguity", "none", "null", "n/a", "no ambiguity")) else value


class VisionFieldCandidate(BaseModel):
    board: str | None = None
    field: str
    value: str | float | int | None = None
    unit: str | None = None
    bbox: list[float] | None = None
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value


class VisionFieldCandidates(BaseModel):
    candidates: list[VisionFieldCandidate] = Field(default_factory=list)
    ambiguity: str | None = None
    rationale: str

    @field_validator("ambiguity", mode="before")
    @classmethod
    def normalise_no_ambiguity(cls, value):
        if not isinstance(value, str):
            return value
        return None if value.strip().lower().startswith(("no ambiguity", "none", "null", "n/a")) else value


class VisionCandidateVerification(BaseModel):
    supported: bool
    board_matches: bool
    field_matches: bool
    value_matches: bool
    unique_match: bool
    confidence: float = Field(ge=0, le=1)
    rationale: str

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value


class VisionViewerVerification(BaseModel):
    supported: bool
    claim_supported: bool
    screenshot_sufficient: bool
    target_visible: bool = False
    confidence: float = Field(ge=0, le=1)
    rationale: str

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_percentage_confidence(cls, value):
        return value / 100 if isinstance(value, (int, float)) and value > 1 else value
