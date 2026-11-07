"""Versioned prediction/outcome records and local ingestion receipts."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_projects import Name
from agent6_mlops_lifecycle.contracts.mlops_storage import StoredEvaluationReference

type ObservationTaskType = Literal["binary_classification", "regression"]
type ObservationValue = (
    Annotated[int, Field(strict=True)] | Annotated[float, Field(strict=True, allow_inf_nan=False)]
)
DigestRef = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0)]


class ScopedObservationRecord(Contract):
    schema_version: Literal["1"] = "1"
    tenant_id: Name
    project_id: Name
    model_id: Name
    model_version: Name
    task_type: ObservationTaskType
    observation_id: Name
    source_ref: Name


class PredictionRecord(ScopedObservationRecord):
    """observed_at is prediction time; recorded_at is when it became available."""

    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "allOf": [
                {
                    "if": {
                        "properties": {"task_type": {"const": "binary_classification"}},
                        "required": ["task_type"],
                    },
                    "then": {"properties": {"predicted": {"type": "integer", "enum": [0, 1]}}},
                },
                {
                    "if": {
                        "properties": {"task_type": {"const": "regression"}},
                        "required": ["task_type"],
                    },
                    "then": {
                        "properties": {
                            "positive_probability": {"type": "null"},
                            "decision_threshold": {"type": "null"},
                        }
                    },
                },
            ],
        }
    )

    observed_at: AwareDatetime
    recorded_at: AwareDatetime
    predicted: ObservationValue
    positive_probability: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = Field(default=None, exclude_if=lambda v: v is None)
    decision_threshold: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = Field(default=None, exclude_if=lambda v: v is None)
    input_ref: DigestRef | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def validate_prediction(self) -> "PredictionRecord":
        if self.recorded_at < self.observed_at:
            raise ValueError("Prediction recorded_at cannot precede observed_at")
        _validate_value(self.task_type, self.predicted)
        if self.task_type != "binary_classification" and self.positive_probability is not None:
            raise ValueError("Positive-class probabilities apply only to binary classification")
        if self.decision_threshold is not None and (
            self.task_type != "binary_classification"
            or self.positive_probability is None
            or self.predicted != int(self.positive_probability >= self.decision_threshold)
        ):
            raise ValueError("Probability, threshold and predicted class differ")
        return self


class OutcomeRecord(ScopedObservationRecord):
    """confirmed_at is when a trusted source made the actual outcome available."""

    confirmed_at: AwareDatetime
    actual: ObservationValue

    @model_validator(mode="after")
    def validate_outcome(self) -> "OutcomeRecord":
        _validate_value(self.task_type, self.actual)
        return self


def _validate_value(task_type: ObservationTaskType, value: int | float) -> None:
    if task_type == "binary_classification" and (type(value) is not int or value not in (0, 1)):
        raise ValueError("Binary observations require integer 0 or 1, not a score")


class ObservationIngestionRequest(Contract):
    schema_version: Literal["1"] = "1"
    tenant_id: Name
    project_id: Name
    model_id: Name
    model_version: Name
    dataset_ref: Name
    window_start: AwareDatetime
    window_end: AwareDatetime
    as_of: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> "ObservationIngestionRequest":
        if self.window_start >= self.window_end:
            raise ValueError("Ingestion window_start must precede window_end")
        if self.as_of < self.window_end:
            raise ValueError("Ingestion as_of must be at or after window_end")
        return self


class LocalObservationSourceConfig(Contract):
    """The CLI resolves paths against the configuration file's directory."""

    query: ObservationIngestionRequest
    predictions_file: Path
    outcomes_file: Path


class ObservationIngestionSummary(Contract):
    prediction_source_ref: DigestRef
    outcome_source_ref: DigestRef
    prediction_count: Count
    window_prediction_count: Count
    available_prediction_count: Count
    matched_count: Count
    late_prediction_count: Count
    late_outcome_count: Count
    duplicate_prediction_count: Count
    duplicate_outcome_count: Count
    pending_observation_ids: Annotated[tuple[Name, ...], Field(max_length=20_000)]
    outcome_coverage: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_counts(self) -> "ObservationIngestionSummary":
        if self.window_prediction_count > self.prediction_count:
            raise ValueError("Window count exceeds prediction count")
        if (
            self.available_prediction_count + self.late_prediction_count
            != self.window_prediction_count
        ):
            raise ValueError("Available and late prediction counts must account for the window")
        if (
            self.matched_count + len(self.pending_observation_ids)
            != self.available_prediction_count
        ):
            raise ValueError("Matched and pending counts must account for available predictions")
        if len(set(self.pending_observation_ids)) != len(self.pending_observation_ids):
            raise ValueError("Pending observation IDs must be unique")
        if self.late_outcome_count > len(self.pending_observation_ids):
            raise ValueError("Late outcomes must belong to pending predictions")
        coverage = (
            self.matched_count / self.available_prediction_count
            if self.available_prediction_count
            else 0.0
        )
        if self.outcome_coverage != coverage:
            raise ValueError("Outcome coverage differs from matched/available counts")
        return self


class ObservationIngestionReceipt(Contract):
    schema_version: Literal["1"] = "1"
    query: ObservationIngestionRequest
    summary: ObservationIngestionSummary
    status: Literal["STORED", "INSUFFICIENT_EVIDENCE"]
    reference: StoredEvaluationReference | None

    @model_validator(mode="after")
    def validate_reference(self) -> "ObservationIngestionReceipt":
        if self.reference is None:
            if self.status != "INSUFFICIENT_EVIDENCE" or self.summary.matched_count != 0:
                raise ValueError("An empty ingestion requires INSUFFICIENT_EVIDENCE")
            return self
        if self.status != "STORED" or self.summary.matched_count == 0:
            raise ValueError("Stored ingestion requires matched observations")
        for field in (
            "tenant_id",
            "project_id",
            "model_id",
            "model_version",
            "dataset_ref",
            "window_start",
            "window_end",
        ):
            if getattr(self.reference, field) != getattr(self.query, field):
                raise ValueError("Stored reference differs from ingestion scope")
        return self
