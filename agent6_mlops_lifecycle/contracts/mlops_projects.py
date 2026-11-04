"""Project-neutral, versioned evidence and evaluation contracts for Agent 6."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import (
    EvaluationEvidenceContext,
    MetricEvaluationPolicy,
    MetricGateResult,
)
from agent6_mlops_lifecycle.contracts.mlops_types import (
    FiniteNumber as FiniteNumber,
)
from agent6_mlops_lifecycle.contracts.mlops_types import (
    Name as Name,
)
from agent6_mlops_lifecycle.contracts.mlops_types import (
    Nonnegative as Nonnegative,
)
from agent6_mlops_lifecycle.contracts.mlops_types import (
    TaskType as TaskType,
)
from agent6_mlops_lifecycle.contracts.mlops_types import (
    UnitScore as UnitScore,
)


class ProjectManifest(Contract):
    """A project registers the exact model and evaluator policy it wants assessed."""

    project_id: Name
    model_id: Name
    model_version: Name
    task_type: TaskType
    evaluator_id: Name
    policy_version: Name
    min_samples: Annotated[int, Field(strict=True, ge=1)] = 30
    min_positive_samples: Annotated[int, Field(strict=True, ge=1)] = 5
    min_f1: UnitScore | None = None
    max_mae: Nonnegative | None = None
    min_text_score: UnitScore | None = None
    allow_external_llm: bool = False
    evaluator_config: dict[str, JsonValue] = Field(default_factory=dict)
    metric_policy: MetricEvaluationPolicy | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def validate_policy(self) -> "ProjectManifest":
        if self.metric_policy is not None:
            if self.evaluator_id != "metric-policy-v2":
                raise ValueError("Metric policies require evaluator_id metric-policy-v2")
            if any(value is not None for value in (self.min_f1, self.max_mae, self.min_text_score)):
                raise ValueError("A v2 metric policy cannot mix legacy threshold fields")
            if self.evaluator_config or self.allow_external_llm:
                raise ValueError("The metric policy evaluator does not consume LLM/plugin settings")
            if self.task_type != "binary_classification" and (
                self.metric_policy.min_positive_samples or self.metric_policy.min_negative_samples
            ):
                raise ValueError("Class support requirements apply only to binary classification")
            return self
        if self.evaluator_id == "metric-policy-v2":
            raise ValueError("metric-policy-v2 requires an explicit versioned metric policy")
        expected = {
            "binary_classification": "min_f1",
            "regression": "max_mae",
            "text_generation": "min_text_score",
            "custom": None,
        }[self.task_type]
        present = {
            name
            for name in ("min_f1", "max_mae", "min_text_score")
            if getattr(self, name) is not None
        }
        required = {expected} if expected is not None else set()
        if present != required:
            raise ValueError(f"{self.task_type} requires only {expected or 'plugin configuration'}")
        if self.task_type != "custom" and self.evaluator_config:
            raise ValueError("evaluator_config is reserved for custom evaluators")
        if self.task_type == "text_generation" and self.min_samples > 100:
            raise ValueError("Text min_samples cannot exceed the evaluator batch limit of 100")
        if self.allow_external_llm and self.task_type != "text_generation":
            raise ValueError("External LLM access applies only to text-generation evaluations")
        return self


class BinaryEvaluationSample(Contract):
    task_type: Literal["binary_classification"] = "binary_classification"
    observation_id: Name
    observed_at: AwareDatetime
    expected: Literal[0, 1]
    predicted: Literal[0, 1]
    positive_probability: (
        Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None
    ) = Field(default=None, exclude_if=lambda v: v is None)


class RegressionEvaluationSample(Contract):
    task_type: Literal["regression"] = "regression"
    observation_id: Name
    observed_at: AwareDatetime
    expected: FiniteNumber
    predicted: FiniteNumber


class CustomEvaluationSample(Contract):
    """Project-specific JSON payload; the registered plugin validates its contents."""

    task_type: Literal["custom"] = "custom"
    observation_id: Name
    observed_at: AwareDatetime
    payload: dict[str, JsonValue]


class TextEvaluationSample(Contract):
    task_type: Literal["text_generation"] = "text_generation"
    observation_id: Name
    observed_at: AwareDatetime
    prompt: Annotated[str, Field(min_length=1, max_length=8000)]
    output: Annotated[str, Field(min_length=1, max_length=8000)]
    reference: Annotated[str, Field(min_length=1, max_length=8000)]
    rubric: Annotated[str, Field(min_length=1, max_length=2000)]


type EvaluationSample = Annotated[
    BinaryEvaluationSample
    | RegressionEvaluationSample
    | TextEvaluationSample
    | CustomEvaluationSample,
    Field(discriminator="task_type"),
]


class EvaluationBatch(Contract):
    project_id: Name
    model_id: Name
    model_version: Name
    dataset_ref: Name
    evidence_ref: Name
    samples: Annotated[tuple[EvaluationSample, ...], Field(min_length=1, max_length=20_000)]
    evidence_context: EvaluationEvidenceContext | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def unique_observations(self) -> "EvaluationBatch":
        ids = [sample.observation_id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("Evaluation observation IDs must be unique")
        if self.evidence_context is not None:
            context = self.evidence_context
            if len(self.samples) > context.available_prediction_count:
                raise ValueError("Labeled sample count exceeds available prediction count")
            if any(
                not context.window_start <= row.observed_at < context.window_end
                for row in self.samples
            ):
                raise ValueError("Samples differ from the evidence context window")
        return self


class TextJudgment(Contract):
    score: UnitScore
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]


class ProjectEvaluationReport(Contract):
    project_id: Name
    model_id: Name
    model_version: Name
    task_type: TaskType
    evaluator_id: Name
    policy_version: Name
    dataset_ref: Name
    evidence_ref: Name
    sample_count: Annotated[int, Field(strict=True, ge=1)]
    decision: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "REVIEW_REQUIRED"]
    metrics: dict[str, FiniteNumber]
    rationale_summary: Annotated[str, Field(min_length=1)]
    requires_human_review: bool
    evaluator_model: str | None = None
    metric_policy_version: Literal["2"] | None = Field(default=None, exclude_if=lambda v: v is None)
    gate_results: tuple[MetricGateResult, ...] = Field(
        default=(), max_length=32, exclude_if=lambda v: not v
    )
    evaluated_at: AwareDatetime | None = Field(default=None, exclude_if=lambda v: v is None)
    outcome_coverage: UnitScore | None = Field(default=None, exclude_if=lambda v: v is None)
    evidence_age_seconds: Nonnegative | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def validate_review_gate(self) -> "ProjectEvaluationReport":
        if not self.requires_human_review:
            raise ValueError("Project evaluations must require review before promotion")
        if self.metric_policy_version is not None:
            if self.evaluator_id != "metric-policy-v2" or self.evaluated_at is None:
                raise ValueError(
                    "Metric-policy reports require their evaluator and evaluation time"
                )
            ids = [result.gate.metric_id for result in self.gate_results]
            if len(ids) != len(set(ids)):
                raise ValueError("Metric gate results must be unique")
            for result in self.gate_results:
                if (
                    result.value is not None
                    and self.metrics.get(result.gate.metric_id) != result.value
                ):
                    raise ValueError("Reported metric differs from its gate result")
            if self.decision == "PASS" and (
                not self.gate_results
                or any(result.status != "PASS" for result in self.gate_results)
            ):
                raise ValueError("PASS requires every metric gate to pass")
            if self.decision == "FAIL" and (
                not any(result.status == "FAIL" for result in self.gate_results)
                or any(result.status == "UNAVAILABLE" for result in self.gate_results)
            ):
                raise ValueError("FAIL requires failed gates without unavailable required metrics")
            expected_metrics = {
                result.gate.metric_id for result in self.gate_results if result.value is not None
            }
            if set(self.metrics) != expected_metrics:
                raise ValueError("Metric report contains missing or unconfigured values")
            if self.gate_results:
                expected_decision = (
                    "INSUFFICIENT_EVIDENCE"
                    if any(result.status == "UNAVAILABLE" for result in self.gate_results)
                    else "FAIL"
                    if any(result.status == "FAIL" for result in self.gate_results)
                    else "PASS"
                )
                if self.decision != expected_decision:
                    raise ValueError("Metric report decision differs from its gate results")
            elif self.decision != "INSUFFICIENT_EVIDENCE":
                raise ValueError("No metric gates were evaluated; evidence must be insufficient")
        elif (
            self.gate_results
            or self.evaluated_at
            or self.outcome_coverage is not None
            or self.evidence_age_seconds is not None
        ):
            raise ValueError("Metric report details require a v2 policy version")
        if self.decision == "REVIEW_REQUIRED" and self.task_type not in (
            "text_generation",
            "custom",
        ):
            raise ValueError("REVIEW_REQUIRED is reserved for text or custom judgments")
        return self
