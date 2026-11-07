"""Bounded contracts for label-free operational health and feature monitoring."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_types import Name

Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0)]
FEATURE_NAMES = frozenset(
    (
        "accountAgeDays",
        "numItems",
        "localTime",
        "paymentMethodAgeDays",
        "paymentMethod",
        "Category",
        "isWeekend",
    )
)


class ServingEvent(Contract):
    schema_version: Literal["1"] = "1"
    event_id: Name
    tenant_id: Name
    project_id: Name
    model_id: Name
    model_version: Name
    observation_id: Name | None = None
    observed_at: AwareDatetime
    recorded_at: AwareDatetime
    channel: Literal["api", "streamlit"]
    duration_ms: Nonnegative
    success: bool
    status_code: Annotated[int, Field(ge=100, le=599)]
    feature_bins: dict[Name, Annotated[str, Field(max_length=64)]]

    @model_validator(mode="after")
    def validate_event(self) -> "ServingEvent":
        if self.recorded_at < self.observed_at:
            raise ValueError("Telemetry availability cannot precede the observation")
        if self.success != (self.status_code < 400):
            raise ValueError("Operation success differs from its HTTP-equivalent status")
        if self.success != (self.observation_id is not None):
            raise ValueError("Successful operations require an observation ID")
        if set(self.feature_bins) != FEATURE_NAMES:
            raise ValueError("Telemetry requires exactly the allowlisted feature buckets")
        return self


class OperationalHealth(Contract):
    model_version: Name
    window_start: AwareDatetime
    window_end: AwareDatetime
    status: Literal["NO_TELEMETRY", "COLLECTING", "HEALTHY", "DEGRADED"]
    attempts: Count
    failures: Count
    failure_rate: Rate | None = None
    input_issue_rate: Rate | None = None
    latency_p50_ms: Nonnegative | None = None
    latency_p95_ms: Nonnegative | None = None
    successful_predictions: Count
    tracked_predictions: Count
    telemetry_coverage: Rate | None = None
    reasons: tuple[Name, ...] = ()


class FeatureShift(Contract):
    status: Literal["WARMING_UP", "NO_TELEMETRY", "INSUFFICIENT_EVIDENCE", "STABLE", "DETECTED"]
    scores: dict[Name, Nonnegative] = Field(default_factory=dict)
    detected_features: tuple[Name, ...] = ()
    threshold: Nonnegative
    reference_samples: Count = 0
    current_samples: Count = 0


class ServiceProbe(Contract):
    state: Literal["UP", "DOWN"]
    checked_at: AwareDatetime
    response_ms: Nonnegative | None = None
    error: str | None = None
