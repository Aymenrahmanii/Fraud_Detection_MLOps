"""Bounded fraud evaluation contracts; unsupported evidence remains explicit."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.paths import ENV_FILE

Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Cost = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0)]


class FraudSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT6_FRAUD_", env_file=ENV_FILE, extra="ignore")
    min_labels: int = Field(default=30, ge=2, le=20000)
    min_positive_labels: int = Field(default=5, ge=1)
    min_negative_labels: int = Field(default=5, ge=1)
    min_coverage: Score = 0.5
    min_recall: Score = 0.9
    max_false_positive_rate: Score = 0.05
    min_probability_labels: int = Field(default=30, ge=2, le=20000)
    min_probability_coverage: Score = 0.9
    min_average_precision: Score | None = None
    false_negative_cost: Cost | None = None
    false_positive_cost: Cost | None = None
    cost_unit: str = Field(default="cost_units", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
    max_cost_per_1000: Cost | None = None
    min_slice_labels: int = Field(default=30, ge=2, le=20000)
    min_slice_class_labels: int = Field(default=5, ge=1)
    slice_features: tuple[
        Literal["paymentMethod", "Category", "accountAgeDays", "paymentMethodAgeDays"], ...
    ] = ("paymentMethod", "Category", "accountAgeDays")

    @model_validator(mode="after")
    def valid_costs(self) -> "FraudSettings":
        if (self.false_negative_cost is None) != (self.false_positive_cost is None):
            raise ValueError("Configure both missed-fraud and false-alarm costs, or neither")
        if self.max_cost_per_1000 is not None and self.false_negative_cost is None:
            raise ValueError("A business-cost gate requires explicitly configured costs")
        if self.min_positive_labels + self.min_negative_labels > self.min_labels:
            raise ValueError("Fraud class support exceeds the minimum labeled sample count")
        if len(set(self.slice_features)) != len(self.slice_features):
            raise ValueError("Fraud slice features must be unique")
        return self


class Confusion(Contract):
    samples: Count
    positive_labels: Count
    negative_labels: Count
    true_positives: Count
    false_positives: Count
    false_negatives: Count
    true_negatives: Count
    precision: Score | None
    recall: Score | None
    f1: Score | None
    false_positive_rate: Score | None
    missed_fraud_rate: Score | None
    precision_interval95: tuple[Score, Score] | None
    recall_interval95: tuple[Score, Score] | None
    false_positive_interval95: tuple[Score, Score] | None
    estimated_error_cost: Cost | None
    cost_per_1000: Cost | None


class CalibrationBin(Contract):
    lower: Score
    upper: Score
    samples: Count
    mean_probability: Score
    observed_fraud_rate: Score


class ThresholdPoint(Contract):
    threshold: Score
    precision: Score | None
    recall: Score | None
    false_positive_rate: Score | None
    cost_per_1000: Cost | None


class ProbabilityQuality(Contract):
    status: Literal["UNAVAILABLE", "INSUFFICIENT_EVIDENCE", "ASSESSED"]
    samples: Count
    labeled_probability_coverage: Score
    positive_labels: Count
    negative_labels: Count
    average_precision: Score | None
    roc_auc: Score | None
    brier_score: Score | None
    expected_calibration_error: Score | None
    calibration_bins: tuple[CalibrationBin, ...]
    precision_recall_curve: tuple[ThresholdPoint, ...]
    threshold_comparison: tuple[ThresholdPoint, ...]
    advisory_cost_threshold: Score | None
    threshold_applied: Literal[False] = False


class FraudSlice(Contract):
    feature: str
    bucket: str
    status: Literal["INSUFFICIENT_EVIDENCE", "PASS", "FAIL"]
    metrics: Confusion


class FraudEvaluation(Contract):
    schema_version: Literal["1"] = "1"
    policy_version: Literal["fraud-risk-v1"] = "fraud-risk-v1"
    policy: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["WAITING_FOR_LABELS", "INSUFFICIENT_EVIDENCE", "PASS", "FAIL"]
    eligible_predictions: Count
    outcome_coverage: Score
    excluded_simulated_outcomes: Count
    duplicate_transactions_excluded: Count
    missing_feature_evidence: Count
    metrics: Confusion
    probabilities: ProbabilityQuality
    slices: tuple[FraudSlice, ...]
    gates: dict[str, Literal["PASS", "FAIL", "NOT_CONFIGURED", "INSUFFICIENT_EVIDENCE"]]
    cost_unit: str | None
    limitations: tuple[str, ...]
