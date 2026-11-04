"""Deterministic matching by scoped ID with explicit availability cutoffs."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, cast

from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    ObservationIngestionRequest,
    ObservationIngestionSummary,
    OutcomeRecord,
    PredictionRecord,
)
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import EvaluationEvidenceContext
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    BinaryEvaluationSample,
    EvaluationBatch,
    EvaluationSample,
    ProjectManifest,
    RegressionEvaluationSample,
)
from agent6_mlops_lifecycle.interfaces.observations import ObservationSnapshot

MAX_WINDOW_PREDICTIONS = 20_000


@dataclass(frozen=True)
class BuiltObservationWindow:
    summary: ObservationIngestionSummary
    batch: EvaluationBatch | None


def _unique[RecordT: (PredictionRecord, OutcomeRecord)](
    records: tuple[RecordT, ...],
    query: ObservationIngestionRequest,
    manifest: ProjectManifest,
    record_type: type[RecordT],
) -> tuple[dict[str, RecordT], int]:
    result: dict[str, RecordT] = {}
    duplicates = 0
    for source_record in records:
        record = record_type.model_validate_json(source_record.model_dump_json())
        if (
            record.tenant_id,
            record.project_id,
            record.model_id,
            record.model_version,
            record.task_type,
        ) != (
            query.tenant_id,
            query.project_id,
            query.model_id,
            query.model_version,
            manifest.task_type,
        ):
            raise ValueError(
                "Observation tenant, project, model version or task differs from scope"
            )
        previous = result.get(record.observation_id)
        if previous is not None:
            if previous != record:
                raise ValueError(
                    "Conflicting duplicate observation; revisions require a new snapshot"
                )
            duplicates += 1
        else:
            result[record.observation_id] = record
    return result, duplicates


def build_observation_window(
    query: ObservationIngestionRequest,
    manifest: ProjectManifest,
    snapshot: ObservationSnapshot,
) -> BuiltObservationWindow:
    query = ObservationIngestionRequest.model_validate_json(query.model_dump_json())
    manifest = ProjectManifest.model_validate_json(manifest.model_dump_json())
    if (manifest.project_id, manifest.model_id, manifest.model_version) != (
        query.project_id,
        query.model_id,
        query.model_version,
    ):
        raise ValueError("Registered manifest differs from ingestion scope")
    if manifest.task_type not in ("binary_classification", "regression"):
        raise ValueError(
            "Observation ingestion currently supports binary classification and regression"
        )
    predictions, prediction_duplicates = _unique(
        snapshot.predictions, query, manifest, PredictionRecord
    )
    outcomes, outcome_duplicates = _unique(snapshot.outcomes, query, manifest, OutcomeRecord)
    for identity, confirmed_outcome in outcomes.items():
        prediction = predictions.get(identity)
        if prediction is None:
            raise ValueError("Outcome has no corresponding prediction in the supplied snapshot")
        if confirmed_outcome.confirmed_at < prediction.observed_at:
            raise ValueError("Outcome confirmation cannot precede its prediction")
    selected = sorted(
        (
            row
            for row in predictions.values()
            if query.window_start <= row.observed_at < query.window_end
        ),
        key=lambda row: (row.observed_at, row.observation_id),
    )
    if len(selected) > MAX_WINDOW_PREDICTIONS:
        raise ValueError("Observation window exceeds the prediction limit")
    samples: list[EvaluationSample] = []
    pending: list[str] = []
    late_predictions = 0
    late_outcomes = 0
    for prediction in selected:
        if prediction.recorded_at > query.as_of:
            late_predictions += 1
            continue
        outcome = outcomes.get(prediction.observation_id)
        if outcome is None or outcome.confirmed_at > query.as_of:
            pending.append(prediction.observation_id)
            late_outcomes += int(outcome is not None)
            continue
        if manifest.task_type == "binary_classification":
            sample: EvaluationSample = BinaryEvaluationSample(
                observation_id=prediction.observation_id,
                observed_at=prediction.observed_at,
                predicted=cast(Literal[0, 1], prediction.predicted),
                expected=cast(Literal[0, 1], outcome.actual),
                positive_probability=prediction.positive_probability,
            )
        else:
            sample = RegressionEvaluationSample(
                observation_id=prediction.observation_id,
                observed_at=prediction.observed_at,
                predicted=prediction.predicted,
                expected=outcome.actual,
            )
        samples.append(sample)
    available = len(selected) - late_predictions
    summary = ObservationIngestionSummary(
        prediction_source_ref=snapshot.prediction_source_ref,
        outcome_source_ref=snapshot.outcome_source_ref,
        prediction_count=len(predictions),
        window_prediction_count=len(selected),
        available_prediction_count=available,
        matched_count=len(samples),
        late_prediction_count=late_predictions,
        late_outcome_count=late_outcomes,
        duplicate_prediction_count=prediction_duplicates,
        duplicate_outcome_count=outcome_duplicates,
        pending_observation_ids=tuple(pending),
        outcome_coverage=len(samples) / available if available else 0.0,
    )
    if not samples:
        return BuiltObservationWindow(summary, None)
    provenance = json.dumps(
        {
            "schema_version": "1",
            "query": query.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    batch = EvaluationBatch(
        project_id=query.project_id,
        model_id=query.model_id,
        model_version=query.model_version,
        dataset_ref=query.dataset_ref,
        evidence_ref=f"sha256:{sha256(provenance).hexdigest()}",
        samples=tuple(samples),
        evidence_context=EvaluationEvidenceContext(
            window_start=query.window_start,
            window_end=query.window_end,
            as_of=query.as_of,
            available_prediction_count=available,
        )
        if manifest.metric_policy is not None
        else None,
    )
    return BuiltObservationWindow(summary, batch)
