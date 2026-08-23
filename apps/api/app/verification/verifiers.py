from __future__ import annotations

from app.schemas.models import (
    Citation,
    Evidence,
    IfcQueryInput,
    IfcQueryResult,
    VerificationStatus,
    VerifierResult,
)
from app.tools.ifc.repository import IfcRepository


class InvariantValidator:
    def validate(self, *, evidence: list[Evidence], citations: list[Citation], disposition: str) -> VerifierResult:
        if disposition == "answered" and not evidence:
            return VerifierResult(
                verifier="invariant",
                passed=False,
                reason="A factual answer cannot be finalized without evidence.",
            )
        if disposition == "answered" and not citations:
            return VerifierResult(
                verifier="invariant",
                passed=False,
                reason="A factual answer cannot be finalized without citations.",
            )
        return VerifierResult(verifier="invariant", passed=True, reason="Answer invariants satisfied.")


class DeterministicVerifier:
    def __init__(self, repository: IfcRepository) -> None:
        self.repository = repository

    def verify(self, query: IfcQueryInput, result: IfcQueryResult) -> VerifierResult:
        recomputed = self.repository.execute(query)
        if recomputed.value != result.value:
            return VerifierResult(
                verifier="deterministic_ifc",
                passed=False,
                reason="Independent deterministic recomputation differs from the primary result.",
                corrected_value=recomputed.value,
                supporting_evidence_ids=[item.id for item in recomputed.evidence],
            )
        return VerifierResult(
            verifier="deterministic_ifc",
            passed=True,
            reason="Independent deterministic recomputation matches the primary result.",
            supporting_evidence_ids=[item.id for item in recomputed.evidence],
        )


class EvidenceVerifier:
    def verify(self, evidence: list[Evidence], confidence_threshold: float) -> VerifierResult:
        if not evidence:
            return VerifierResult(verifier="evidence", passed=False, reason="No evidence was produced.")
        low_confidence = [
            item for item in evidence
            if item.confidence is not None and item.confidence < confidence_threshold
        ]
        if low_confidence:
            return VerifierResult(
                verifier="evidence",
                passed=False,
                reason="Evidence confidence is below the configured threshold.",
                supporting_evidence_ids=[item.id for item in evidence],
            )
        return VerifierResult(
            verifier="evidence",
            passed=True,
            reason="Evidence record is present and satisfies the MVP confidence threshold.",
            supporting_evidence_ids=[item.id for item in evidence],
        )


def verification_status(results: list[VerifierResult]) -> VerificationStatus:
    passed = all(item.passed for item in results)
    return VerificationStatus(
        status="passed" if passed else "failed",
        verifier_results=results,
        reason=None if passed else next(item.reason for item in results if not item.passed),
    )

