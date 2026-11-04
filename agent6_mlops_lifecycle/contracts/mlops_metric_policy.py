"""Explicit v2 metric policies, evidence requirements and auditable gate results."""

from math import isfinite
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_types import Name, TaskType

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
UnitScore = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]

MetricId = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$", max_length=160)
]
type MetricOperator = Literal["gte", "lte"]


class MetricBaseline(Contract):
    value: FiniteNumber
    evidence_ref: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    max_degradation: Nonnegative


class MetricGate(Contract):
    metric_id: MetricId
    operator: MetricOperator
    threshold: FiniteNumber
    baseline: MetricBaseline | None = None


class MetricEvaluationPolicy(Contract):
    schema_version: Literal["2"] = "2"
    gates: Annotated[tuple[MetricGate, ...], Field(min_length=1, max_length=32)]
    min_positive_samples: Annotated[int, Field(strict=True, ge=0)] = 0
    min_negative_samples: Annotated[int, Field(strict=True, ge=0)] = 0
    min_outcome_coverage: UnitScore | None = None
    max_evidence_age_seconds: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def validate_gates(self) -> "MetricEvaluationPolicy":
        ids = [gate.metric_id for gate in self.gates]
        if len(ids) != len(set(ids)):
            raise ValueError("Each metric may appear only once in a policy")
        return self


class MetricDefinition(Contract):
    metric_id: MetricId
    task_types: Annotated[tuple[TaskType, ...], Field(min_length=1)]
    direction: MetricOperator
    description: Name
    units: Name
    minimum: FiniteNumber | None = None
    maximum: FiniteNumber | None = None
    requires_positive_probability: bool = False

    @model_validator(mode="after")
    def validate_bounds(self) -> "MetricDefinition":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Metric domain minimum exceeds maximum")
        if self.requires_positive_probability and self.task_types != ("binary_classification",):
            raise ValueError("Positive-probability inputs require a binary-only metric")
        if len(set(self.task_types)) != len(self.task_types):
            raise ValueError("Metric task types must be unique")
        return self


class MetricValue(Contract):
    value: FiniteNumber | None = None
    unavailable_reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "MetricValue":
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("Metric requires either a finite value or an unavailable reason")
        return self


class EvaluationEvidenceContext(Contract):
    """Counts refer to all available predictions, not just the labeled subset."""

    window_start: AwareDatetime
    window_end: AwareDatetime
    as_of: AwareDatetime
    available_prediction_count: Annotated[int, Field(strict=True, ge=1, le=20_000)]

    @model_validator(mode="after")
    def validate_window(self) -> "EvaluationEvidenceContext":
        if not self.window_start < self.window_end <= self.as_of:
            raise ValueError("Evidence requires window_start < window_end <= as_of")
        return self


class MetricGateResult(Contract):
    gate: MetricGate
    value: FiniteNumber | None
    status: Literal["PASS", "FAIL", "UNAVAILABLE"]
    unavailable_reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    degradation: FiniteNumber | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "MetricGateResult":
        if self.status == "UNAVAILABLE":
            if (
                self.value is not None
                or self.degradation is not None
                or not self.unavailable_reason
            ):
                raise ValueError("Unavailable gate requires a reason and no fabricated value")
            return self
        if self.value is None or self.unavailable_reason is not None:
            raise ValueError("Available gate requires a value and no unavailable reason")
        absolute_pass = (
            self.value >= self.gate.threshold
            if self.gate.operator == "gte"
            else self.value <= self.gate.threshold
        )
        baseline_pass = True
        if self.gate.baseline is None:
            if self.degradation is not None:
                raise ValueError("A degradation value requires a baseline")
        else:
            expected_drop = (
                self.gate.baseline.value - self.value
                if self.gate.operator == "gte"
                else self.value - self.gate.baseline.value
            )
            if not isfinite(expected_drop) or self.degradation != expected_drop:
                raise ValueError("Gate degradation differs from its baseline and value")
            baseline_pass = expected_drop <= self.gate.baseline.max_degradation
        expected_status = "PASS" if absolute_pass and baseline_pass else "FAIL"
        if self.status != expected_status:
            raise ValueError("Gate status differs from its threshold/baseline result")
        return self
