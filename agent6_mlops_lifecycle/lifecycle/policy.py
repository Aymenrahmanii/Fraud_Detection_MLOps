"""Measured candidate evidence and promotion decisions; LLM output is never an input."""

from math import isclose, isfinite
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings

Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class GuardMetrics(Contract):
    f1: Score
    precision: Score
    recall: Score
    balanced_accuracy: Score
    false_positive_rate: Score | None
    samples: Count
    positive_labels: Count
    tp: Count
    fp: Count
    fn: Count
    tn: Count

    @model_validator(mode="after")
    def check_counts(self) -> "GuardMetrics":
        if (
            self.tp + self.fp + self.fn + self.tn != self.samples
            or self.tp + self.fn != self.positive_labels
        ):
            raise ValueError("Guard confusion counts differ from sample support")
        expected_precision = self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0
        expected_recall = self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0
        denominator = 2 * self.tp + self.fp + self.fn
        expected_f1 = 2 * self.tp / denominator if denominator else 0.0
        specificity = self.tn / (self.tn + self.fp) if self.tn + self.fp else 0.0
        expected_balanced = (
            (expected_recall + specificity) / 2
            if self.positive_labels and self.samples > self.positive_labels
            else expected_recall
            if self.positive_labels
            else specificity
        )
        for measured, expected in (
            (self.precision, expected_precision),
            (self.recall, expected_recall),
            (self.f1, expected_f1),
            (self.balanced_accuracy, expected_balanced),
        ):
            if not isclose(measured, expected, abs_tol=1e-12):
                raise ValueError("Guard metric differs from its confusion counts")
        expected_fpr = self.fp / (self.fp + self.tn) if self.fp + self.tn else None
        if (expected_fpr is None) != (self.false_positive_rate is None) or (
            expected_fpr is not None
            and not isclose(self.false_positive_rate or 0, expected_fpr, abs_tol=1e-12)
        ):
            raise ValueError("False-positive rate differs from its confusion counts")
        return self


class CandidateReport(Contract):
    qualified: Annotated[bool, Field(strict=True)]
    improvement_ci95: tuple[float, float] | None
    gates: dict[str, Annotated[bool, Field(strict=True)]]
    historical: GuardMetrics
    candidate_live: GuardMetrics | None
    champion_live: GuardMetrics | None
    verified_live_guard: Annotated[bool, Field(strict=True)]
    guard_origin: Literal[
        "HISTORICAL_ONLY", "VERIFIED_BUSINESS_OUTCOMES", "ISOLATED_VERIFICATION_FIXTURES"
    ]
    training_source_sha256: Digest
    benchmark_dataset_id: Digest
    training_independence: Literal["GUARD_FEATURES_EXCLUDED"]
    fit_feature_hashes_sha256: Digest
    fit_samples: Count
    validation_samples: Count
    production_training_rows: Count
    production_guard_rows: Count
    threshold: Score
    candidate_path: str
    candidate_sha256: Digest
    mlflow_run_id: str
    registered_model: Literal["fraud-agent6-candidates"]
    registered_version: str | None

    @model_validator(mode="after")
    def validate_evidence(self) -> "CandidateReport":
        required = {
            "minimum_f1",
            "minimum_recall",
            "false_positive_rate",
            "historical_regression_guard",
            "champion_improvement",
            "champion_recall_guard",
        }
        if set(self.gates) != required or self.qualified != all(self.gates.values()):
            raise ValueError("Candidate qualification differs from its measured gates")
        if self.verified_live_guard != (
            self.candidate_live is not None and self.champion_live is not None
        ):
            raise ValueError("Verified live guard metadata is inconsistent")
        if self.candidate_live and self.production_guard_rows != self.candidate_live.samples:
            raise ValueError("Guard support differs from its evaluated labeled sample count")
        if (self.guard_origin == "HISTORICAL_ONLY") == self.verified_live_guard:
            raise ValueError("Guard provenance and labeled evidence differ")
        if self.improvement_ci95 and (
            not all(isfinite(value) and -1 <= value <= 1 for value in self.improvement_ci95)
            or self.improvement_ci95[0] > self.improvement_ci95[1]
        ):
            raise ValueError("Invalid measured improvement interval")
        if self.qualified != (self.registered_version is not None):
            raise ValueError("Only qualified candidates may be registered for promotion")
        return self


def promotion_decision(report: CandidateReport, policy: LifecycleSettings) -> tuple[str, str]:
    selected = report.candidate_live or report.historical
    champion = report.champion_live
    expected = {
        "minimum_f1": selected.f1 >= policy.min_f1,
        "minimum_recall": selected.recall >= policy.min_recall,
        "false_positive_rate": selected.false_positive_rate is not None
        and selected.false_positive_rate <= policy.max_false_positive_rate,
        "historical_regression_guard": report.historical.f1 >= policy.min_f1
        and report.historical.recall >= policy.min_recall,
        "champion_improvement": champion is None
        or selected.f1 >= champion.f1 + policy.min_f1_improvement,
        "champion_recall_guard": champion is None
        or selected.recall >= champion.recall - policy.max_recall_drop,
    }
    if report.gates != expected:
        raise ValueError("Worker gate results differ from the exact configured promotion policy")
    if not report.qualified:
        return "REJECTED", "Candidate failed measured quality/regression gates; champion retained"
    supported = bool(
        report.verified_live_guard
        and report.guard_origin == "VERIFIED_BUSINESS_OUTCOMES"
        and report.production_guard_rows >= policy.min_guard_labels
        and report.candidate_live is not None
        and min(
            report.candidate_live.positive_labels,
            report.candidate_live.samples - report.candidate_live.positive_labels,
        )
        >= policy.min_guard_class_samples
        and report.improvement_ci95
        and report.improvement_ci95[0] > 0
    )
    if policy.auto_promote and policy.policy_calibrated and supported:
        return (
            "DEPLOYMENT_READY",
            "Calibrated policy and supported independent guard permit automatic handoff",
        )
    return (
        "AWAITING_REVIEW",
        (
            "Consequential promotion requires review: calibration or statistical/production "
            "evidence is incomplete"
        ),
    )
