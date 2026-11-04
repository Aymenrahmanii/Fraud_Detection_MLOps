"""Versioned monitoring reports and independently attributed outcome requests."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_ingestion import ObservationIngestionReceipt
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectEvaluationReport
from agent6_mlops_lifecycle.contracts.mlops_types import Name
from agent6_mlops_lifecycle.fraud.models import FraudEvaluation
from agent6_mlops_lifecycle.monitoring.business_models import OutcomeFeedStatus
from agent6_mlops_lifecycle.monitoring.telemetry_models import (
    FeatureShift,
    OperationalHealth,
    ServiceProbe,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0)]


class MonitorScope(Contract):
    tenant_id: Name = "local-fraud"
    project_id: Name = "fraud-detection-serving"
    model_id: Name = "fraud-api-pipeline"


class OutcomeSubmission(Contract):
    actual: Annotated[int, Field(strict=True, ge=0, le=1)]
    note: Annotated[str, Field(min_length=3, max_length=1000)]
    verified: Annotated[bool, Field(strict=True)]
    kind: Literal["HUMAN", "SIMULATED", "SYSTEM"] = "HUMAN"
    event_id: Name | None = Field(default=None, exclude_if=lambda value: value is None)
    evidence_ref: Name | None = Field(default=None, exclude_if=lambda value: value is None)
    occurred_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def require_verification(self) -> "OutcomeSubmission":
        if not self.verified:
            raise ValueError("Outcome confirmation requires independent verification")
        if self.kind == "SYSTEM" and any(
            value is None for value in (self.event_id, self.evidence_ref, self.occurred_at)
        ):
            raise ValueError("System outcomes require an event ID, evidence and occurrence time")
        if self.kind != "SYSTEM" and any(
            value is not None for value in (self.event_id, self.evidence_ref, self.occurred_at)
        ):
            raise ValueError("Business event metadata is reserved for system outcomes")
        return self


class OutcomeAudit(Contract):
    observation_id: Name
    reviewer_id: Name
    submission: OutcomeSubmission
    confirmed_at: AwareDatetime


class DriftBaseline(Contract):
    scope: MonitorScope
    model_version: Name
    prediction_counts: tuple[Count, Count]
    prediction_source_ref: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    window_start: AwareDatetime
    window_end: AwareDatetime
    approved_at: AwareDatetime
    approved_by: Name
    note: Annotated[str, Field(min_length=3, max_length=1000)]
    origin: Literal["OPERATOR", "AUTO_WARMUP"] = "OPERATOR"
    feature_counts: dict[Name, dict[Name, Count]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_window(self) -> "DriftBaseline":
        if not self.window_start < self.window_end <= self.approved_at:
            raise ValueError("Baseline approval requires a completed reference window")
        if sum(self.prediction_counts) == 0:
            raise ValueError("A baseline requires actual observations")
        return self


class PredictionDrift(Contract):
    status: Literal["NO_BASELINE", "INSUFFICIENT_EVIDENCE", "STABLE", "DETECTED"]
    score: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    threshold: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reference_samples: Count = 0
    current_samples: Count
    baseline_sha256: Digest | None = None

    @model_validator(mode="after")
    def validate_score(self) -> "PredictionDrift":
        available = self.status in ("STABLE", "DETECTED")
        if available != (self.score is not None):
            raise ValueError("Only an eligible drift comparison can have a score")
        if self.score is not None and (
            (self.score >= self.threshold) != (self.status == "DETECTED")
        ):
            raise ValueError("Drift status differs from its measured threshold")
        return self


class MonitoringReport(Contract):
    schema_version: Literal["1"] = "1"
    report_id: Digest
    data_signature: Digest
    scope: MonitorScope
    model_version: Name
    assessed_at: AwareDatetime
    prediction_counts: tuple[Count, Count]
    simulated_outcomes: Count
    human_outcomes: Count
    system_outcomes: Count = 0
    feature_drift: FeatureShift | None = None
    operational_health: OperationalHealth | None = None
    ingestion: ObservationIngestionReceipt
    evaluation: ProjectEvaluationReport | None
    fraud_evaluation: FraudEvaluation | None = None
    task_status: Literal["SUCCEEDED", "NOT_RUN"]
    drift: PredictionDrift
    recommendation: Literal[
        "COLLECT_LABELS", "REVIEW_MODEL", "REVIEW_SHIFT", "CONTINUE_MONITORING", "REVIEW_OPERATIONS"
    ]
    policy_is_calibrated: Literal[False] = False
    automatic_deployment: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts(self) -> "MonitoringReport":
        if sum(self.prediction_counts) != self.ingestion.summary.available_prediction_count:
            raise ValueError("Report prediction counts differ from the ingested snapshot")
        if (
            self.human_outcomes + self.simulated_outcomes + self.system_outcomes
            != self.ingestion.summary.matched_count
        ):
            raise ValueError("Outcome provenance counts differ from matched evidence")
        if (self.evaluation is not None) != (self.task_status == "SUCCEEDED"):
            raise ValueError("Evaluation details require successful Agent 6 task execution")
        if self.evaluation is not None and (
            self.ingestion.reference is None
            or self.evaluation.evidence_ref != self.ingestion.reference.evidence_ref
        ):
            raise ValueError("Evaluation differs from ingested evidence")
        return self


class WorkerStatus(Contract):
    state: Literal[
        "STARTING",
        "WAITING_FOR_SOURCE",
        "WAITING_FOR_PREDICTIONS",
        "MONITORING",
        "ERROR",
        "STOPPED",
    ]
    polled_at: AwareDatetime
    last_success_at: AwareDatetime | None = None
    error: str | None = None
    active_versions: tuple[Name, ...] = ()
    outcome_feed: OutcomeFeedStatus | None = None
    operations: dict[Name, OperationalHealth] = Field(default_factory=dict)
    services: dict[Name, ServiceProbe] = Field(default_factory=dict)
