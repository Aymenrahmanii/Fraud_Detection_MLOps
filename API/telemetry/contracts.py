"""Standalone producer contract compatible with Agent 6 PredictionRecord v1."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=160)]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: Name = "local-fraud"
    project_id: Name = "fraud-detection-serving"
    model_id: Name = "fraud-api-pipeline"


class PredictionEvent(Scope):
    schema_version: Literal["1"] = "1"
    model_version: Name
    task_type: Literal["binary_classification"] = "binary_classification"
    observation_id: Name
    source_ref: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    observed_at: AwareDatetime
    recorded_at: AwareDatetime
    predicted: Annotated[int, Field(strict=True, ge=0, le=1)]
    positive_probability: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = Field(default=None, exclude_if=lambda v: v is None)
    decision_threshold: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = Field(default=None, exclude_if=lambda v: v is None)
    input_ref: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def validate_times(self) -> "PredictionEvent":
        if self.recorded_at < self.observed_at:
            raise ValueError("Prediction availability cannot precede observation")
        if self.decision_threshold is not None and (
            self.positive_probability is None
            or self.predicted
            != int(self.positive_probability >= self.decision_threshold)
        ):
            raise ValueError("Probability, threshold and predicted class differ")
        return self


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
