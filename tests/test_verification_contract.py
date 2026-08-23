"""Deterministic contract tests for app.verification.verifiers (SPEC-M1 §4.4/15).

Pure functions/classes only; the deterministic verifier is driven against
the committed synthetic public fixture (no provider, no network).
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.schemas.models import (
    Citation,
    Evidence,
    IfcQueryInput,
    SourceType,
    VerifierResult,
)
from app.tools.ifc.repository import IfcRepository
from app.verification.verifiers import (
    DeterministicVerifier,
    EvidenceVerifier,
    InvariantValidator,
    verification_status,
)

ROOT = Path(__file__).resolve().parents[1]


def _evidence(**overrides) -> Evidence:
    defaults = dict(
        source_type=SourceType.IFC, source_file="armie_demo.ifc", summary="4 doors",
        locator={"entity_type": "IfcDoor"}, extracted_value=4, confidence=0.95,
    )
    defaults.update(overrides)
    return Evidence(**defaults)


def _citation(evidence: Evidence) -> Citation:
    return Citation(evidence_id=evidence.id, source_type=evidence.source_type, label=evidence.summary, locator=evidence.locator)


# --- InvariantValidator ------------------------------------------------------------

def test_invariant_validator_fails_answered_without_evidence() -> None:
    result = InvariantValidator().validate(evidence=[], citations=[], disposition="answered")
    assert result.passed is False
    assert "evidence" in result.reason.lower()


def test_invariant_validator_fails_answered_without_citations() -> None:
    evidence = [_evidence()]
    result = InvariantValidator().validate(evidence=evidence, citations=[], disposition="answered")
    assert result.passed is False
    assert "citation" in result.reason.lower()


def test_invariant_validator_passes_answered_with_evidence_and_citations() -> None:
    evidence = [_evidence()]
    citations = [_citation(evidence[0])]
    result = InvariantValidator().validate(evidence=evidence, citations=citations, disposition="answered")
    assert result.passed is True


def test_invariant_validator_passes_non_answered_disposition_without_evidence() -> None:
    result = InvariantValidator().validate(evidence=[], citations=[], disposition="clarification_required")
    assert result.passed is True


# --- DeterministicVerifier -----------------------------------------------------------

def _repository() -> IfcRepository:
    settings = Settings(data_dir=ROOT / "demo_data", ifc_file="armie_demo.ifc", pdf_file="armie_demo_schedule.pdf")
    return IfcRepository(settings.ifc_path)


def test_deterministic_verifier_agrees_with_recomputed_result() -> None:
    repository = _repository()
    query = IfcQueryInput(operation="count", entity_type="IfcDoor")
    result = repository.execute(query)
    verifier_result = DeterministicVerifier(repository).verify(query, result)
    assert verifier_result.passed is True
    assert verifier_result.corrected_value is None


def test_deterministic_verifier_flags_injected_disagreement_and_populates_corrected_value() -> None:
    repository = _repository()
    query = IfcQueryInput(operation="count", entity_type="IfcDoor")
    real_result = repository.execute(query)
    tampered_result = real_result.model_copy(update={"value": real_result.value + 999})
    verifier_result = DeterministicVerifier(repository).verify(query, tampered_result)
    assert verifier_result.passed is False
    assert verifier_result.corrected_value == real_result.value
    assert verifier_result.corrected_value != tampered_result.value


# --- EvidenceVerifier ------------------------------------------------------------------

def test_evidence_verifier_fails_on_empty_evidence() -> None:
    result = EvidenceVerifier().verify([], confidence_threshold=0.75)
    assert result.passed is False
    assert "no evidence" in result.reason.lower()


def test_evidence_verifier_fails_below_threshold() -> None:
    evidence = [_evidence(confidence=0.5)]
    result = EvidenceVerifier().verify(evidence, confidence_threshold=0.75)
    assert result.passed is False


def test_evidence_verifier_passes_at_threshold() -> None:
    evidence = [_evidence(confidence=0.75)]
    result = EvidenceVerifier().verify(evidence, confidence_threshold=0.75)
    assert result.passed is True


def test_evidence_verifier_passes_above_threshold() -> None:
    evidence = [_evidence(confidence=0.99)]
    result = EvidenceVerifier().verify(evidence, confidence_threshold=0.75)
    assert result.passed is True


def test_evidence_verifier_passes_none_confidence() -> None:
    evidence = [_evidence(confidence=None)]
    result = EvidenceVerifier().verify(evidence, confidence_threshold=0.75)
    assert result.passed is True


# --- verification_status ----------------------------------------------------------------

def test_verification_status_passed_when_all_verifiers_pass() -> None:
    results = [VerifierResult(verifier="a", passed=True, reason="ok"), VerifierResult(verifier="b", passed=True, reason="ok")]
    status = verification_status(results)
    assert status.status == "passed"
    assert status.reason is None


def test_verification_status_failed_when_any_verifier_fails_and_surfaces_first_reason() -> None:
    results = [
        VerifierResult(verifier="a", passed=True, reason="ok"),
        VerifierResult(verifier="b", passed=False, reason="disagreement detected"),
    ]
    status = verification_status(results)
    assert status.status == "failed"
    assert status.reason == "disagreement detected"
