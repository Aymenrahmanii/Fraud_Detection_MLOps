"""Persist and assess actual offline binary predictions through the reference task path."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent6_mlops_lifecycle.adapters.jsonl_observations import JSONLObservationSource
from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.enums import AgentId, Environment, ResultStatus
from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    LocalObservationSourceConfig,
    ObservationIngestionRequest,
    OutcomeRecord,
    PredictionRecord,
)
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import MetricEvaluationPolicy
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectManifest
from agent6_mlops_lifecycle.contracts.tasks import AgentTask, TaskContext
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.ingestion.service import ObservationIngestionService
from agent6_mlops_lifecycle.service import MLOpsLifecycleAgent


def evaluate_offline_binary(
    *,
    output_dir: Path,
    project_id: str,
    model_id: str,
    model_sha256: str,
    dataset_sha256: str,
    row_indices: tuple[int, ...],
    predictions: tuple[int, ...],
    labels: tuple[int, ...],
    observed_at: datetime,
    available_at: datetime,
    metric_policy: MetricEvaluationPolicy,
    tenant_id: str = "local-fraud",
    min_samples: int = 100,
) -> dict[str, object]:
    """Availability times describe this offline import, not historical business events.

    Source-model inference remains in the project's compatible runtime. This bridge
    sends only predictions and independently supplied CSV labels to Agent 6.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", model_sha256) or not re.fullmatch(
        r"[0-9a-f]{64}", dataset_sha256
    ):
        raise ValueError("Model and dataset need full SHA-256 provenance")
    if not row_indices or not len(row_indices) == len(predictions) == len(labels):
        raise ValueError("CSV row IDs, predictions and labels must have equal nonzero lengths")
    if len(row_indices) > 20_000:
        raise ValueError("Offline evaluation is limited to 20,000 rows per cohort")
    if len(set(row_indices)) != len(row_indices) or any(
        type(index) is not int or index < 0 for index in row_indices
    ):
        raise ValueError("CSV row positions must be unique nonnegative integers")
    if any(type(value) is not int or value not in (0, 1) for value in (*predictions, *labels)):
        raise ValueError("Offline binary predictions and labels must be integer 0 or 1")
    if observed_at.tzinfo is None or available_at.tzinfo is None or available_at < observed_at:
        raise ValueError("Offline scoring requires ordered timezone-aware times")
    metric_policy = MetricEvaluationPolicy.model_validate_json(metric_policy.model_dump_json())
    manifest = ProjectManifest(
        project_id=project_id,
        model_id=model_id,
        model_version="artifact-" + model_sha256,
        task_type="binary_classification",
        evaluator_id="metric-policy-v2",
        policy_version="offline-binary-v2",
        min_samples=min_samples,
        metric_policy=metric_policy,
    )
    # Validate the metric IDs/directions before creating any persisted files.
    admission = ProjectEvaluationService(tenant_id=tenant_id)
    admission.register_project(manifest)
    scope = dict(
        tenant_id=tenant_id,
        project_id=project_id,
        model_id=model_id,
        model_version=manifest.model_version,
        task_type="binary_classification",
        source_ref="sha256:" + dataset_sha256,
    )
    prediction_records = []
    outcome_records = []
    for index, predicted, actual in zip(row_indices, predictions, labels, strict=True):
        observation_id = f"csv:{dataset_sha256}:row:{index}"
        prediction_records.append(
            PredictionRecord.model_validate(
                {
                    **scope,
                    "observation_id": observation_id,
                    "observed_at": observed_at,
                    "recorded_at": available_at,
                    "predicted": predicted,
                }
            )
        )
        outcome_records.append(
            OutcomeRecord.model_validate(
                {
                    **scope,
                    "observation_id": observation_id,
                    "confirmed_at": available_at,
                    "actual": actual,
                }
            )
        )
    query = ObservationIngestionRequest(
        tenant_id=tenant_id,
        project_id=project_id,
        model_id=model_id,
        model_version=manifest.model_version,
        dataset_ref=f"offline-csv-{dataset_sha256}",
        window_start=observed_at,
        window_end=max(available_at, observed_at + timedelta(microseconds=1)),
        as_of=max(available_at, observed_at + timedelta(microseconds=1)),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    # An evidence export directory is a snapshot: never replace an existing run.
    files = {
        "manifest.json": manifest.model_dump_json(indent=2),
        "predictions.jsonl": "".join(row.model_dump_json() + "\n" for row in prediction_records),
        "outcomes.jsonl": "".join(row.model_dump_json() + "\n" for row in outcome_records),
        "sources.json": LocalObservationSourceConfig(
            query=query,
            predictions_file=Path("predictions.jsonl"),
            outcomes_file=Path("outcomes.jsonl"),
        ).model_dump_json(indent=2),
    }
    if any((output_dir / name).exists() for name in (*files, "projects.sqlite3")):
        raise ValueError("Offline export already exists; select a new output directory")
    for name, contents in files.items():
        with (output_dir / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
    database = output_dir / "projects.sqlite3"
    store = SQLiteProjectStore(database)
    store.register_manifest(tenant_id, manifest)
    ingestion = ObservationIngestionService(manifests=store, batches=store, tenant_id=tenant_id)
    receipt = ingestion.ingest(
        query,
        JSONLObservationSource(output_dir / "predictions.jsonl", output_dir / "outcomes.jsonl"),
    )
    if receipt.reference is None:
        raise RuntimeError("Actual offline labels unexpectedly produced no evaluation evidence")
    (output_dir / "ingestion-receipt.json").write_text(
        receipt.model_dump_json(indent=2), encoding="utf-8"
    )
    reference = receipt.reference
    (output_dir / "reference.json").write_text(
        reference.model_dump_json(indent=2), encoding="utf-8"
    )
    # Reopen the database to exercise the same durable reference path used by AgentTasks.
    reopened = SQLiteProjectStore(database)
    agent = MLOpsLifecycleAgent(
        project_evaluation=ProjectEvaluationService(
            manifests=reopened, batches=reopened, tenant_id=tenant_id
        )
    )
    task = AgentTask(
        task_id="offline-" + reference.evidence_ref.removeprefix("sha256:")[:24],
        incident_id="offline-binary-evaluation",
        agent_id=AgentId.MLOPS_LIFECYCLE,
        objective="Assess actual offline model predictions using saved evidence",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        task_context=TaskContext(
            service_id=project_id,
            namespace="offline",
            environment=Environment.TEST,
            signals={"project_evaluation_ref": reference.model_dump(mode="json")},
            evidence_refs=(reference.evidence_ref,),
        ),
    )
    result = asyncio.run(agent.handle(task))
    if result.status != ResultStatus.SUCCEEDED:
        raise RuntimeError("Agent 6 could not evaluate the stored offline evidence")
    (output_dir / "agent-result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "evaluation-report.json").write_text(
        json.dumps(result.payload, indent=2, allow_nan=False), encoding="utf-8"
    )
    return {
        "database": str(database.resolve()),
        "tenant_id": tenant_id,
        "reference": reference.model_dump(mode="json"),
        "ingestion": receipt.summary.model_dump(mode="json"),
        "task_status": result.status.value,
        "evaluation": result.payload,
        "provenance": {
            "mode": "offline_actual_model",
            "dataset_sha256": dataset_sha256,
            "model_artifact_sha256": model_sha256,
            "csv_row_indices_zero_based": row_indices,
            "timestamp_semantics": "Measured batch scoring and CSV import availability times",
            "labels": "Existing CSV labels; independent production confirmation is unverified",
            "policy": "Illustrative thresholds requiring project-owner calibration",
        },
    }
