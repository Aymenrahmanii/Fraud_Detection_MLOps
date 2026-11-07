"""Immutable benchmark evidence; offline scores never stand in for live correctness."""

from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectEvaluationReport


class BenchmarkRow(Contract):
    inputs: dict[str, JsonValue] = Field(min_length=1, max_length=32)
    actual: Literal[0, 1]
    group: str = Field(min_length=1, max_length=80)


class BenchmarkDataset(Contract):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_name: str = Field(min_length=1, max_length=160)
    label_source: Literal["PROJECT_DATASET"] = "PROJECT_DATASET"
    training_independence: Literal["UNVERIFIED"] = "UNVERIFIED"
    rows: tuple[BenchmarkRow, ...] = Field(min_length=2, max_length=20000)


class ModelArtifact(Contract):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["mlflow", "joblib"]
    path: Path


class BenchmarkRegistry(Contract):
    project_root: Path
    dataset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    models: tuple[ModelArtifact, ...] = Field(min_length=1, max_length=8)


class BenchmarkReport(Contract):
    model_version: str = Field(pattern=r"^artifact-[a-f0-9]{64}$")
    artifact_name: str
    dataset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    assessed_at: AwareDatetime
    source_sha256: str
    label_source: Literal["PROJECT_DATASET"] = "PROJECT_DATASET"
    training_independence: Literal["UNVERIFIED"] = "UNVERIFIED"
    production_correctness: Literal["UNKNOWN"] = "UNKNOWN"
    label_counts: dict[str, tuple[int, int]] = Field(default_factory=dict)
    evaluations: dict[str, ProjectEvaluationReport]
    limitations: tuple[str, ...] = (
        "Labels come from the project's existing dataset, not newly verified payment outcomes.",
        "The artifact has no training-membership manifest; test independence is unverified.",
        "Groups with very few positive labels have unstable precision and recall estimates.",
        "The combined sample was deliberately stratified; it is not live fraud prevalence.",
        "Repeated scores on a fixed benchmark do not establish current production accuracy.",
        "Thresholds are illustrative and require business calibration.",
    )


class AdvisorFinding(Contract):
    check_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    text: str = Field(min_length=1, max_length=1200)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=10)


class AdvisorExplanation(Contract):
    summary: str = Field(min_length=1, max_length=2000)
    findings: tuple[AdvisorFinding, ...] = Field(min_length=1, max_length=10)
    recommended_checks: tuple[str, ...] = Field(min_length=1, max_length=8)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=8)


class AdvisorySelection(Contract):
    check_id: Literal[
        "REVIEW_BENCHMARK_GATES",
        "VERIFY_TRAINING_INDEPENDENCE",
        "COLLECT_VERIFIED_OUTCOMES",
        "BUILD_DRIFT_REFERENCE",
        "CHECK_SERVICE_HEALTH",
        "INSPECT_SERVING_HEALTH",
        "REVIEW_DISTRIBUTION_SHIFT",
        "CALIBRATE_POLICY",
    ]
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=10)


class AdvisoryPlan(Contract):
    findings: tuple[AdvisorySelection, ...] = Field(min_length=1, max_length=8)
