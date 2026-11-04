"""Read-only bounded snapshots of live predictions for Agent 6 JSONL ingestion."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from API.telemetry.contracts import PredictionEvent, Scope, utc_text
from API.telemetry.store import MAX_RECORD_BYTES, PredictionLogError, verify_database

MAX_RECORDS = 20_000
MAX_EXPORT_BYTES = 16 * 1024 * 1024


def export_predictions(
    *,
    database: Path,
    output_dir: Path,
    scope: Scope,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
    model_version: str | None = None,
) -> dict[str, object]:
    start, end, cutoff = (
        utc_text(value) for value in (window_start, window_end, as_of)
    )
    if not start < end <= cutoff:
        raise ValueError("Snapshot requires window_start < window_end <= as_of")
    database = database.resolve(strict=True)
    if not database.is_file():
        raise ValueError("Prediction database must be a file")
    try:
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            verify_database(connection)
            rows = connection.execute(
                "SELECT observation_id,tenant_id,project_id,model_id,"
                "model_version,observed_at,"
                "CASE WHEN length(payload)<=? THEN payload ELSE NULL END "
                "AS payload,digest "
                "FROM predictions WHERE tenant_id=? AND project_id=? AND model_id=? "
                "AND observed_at>=? AND observed_at<? "
                "AND (? IS NULL OR model_version=?) "
                "ORDER BY observed_at,observation_id LIMIT ?",
                (
                    MAX_RECORD_BYTES,
                    scope.tenant_id,
                    scope.project_id,
                    scope.model_id,
                    start,
                    end,
                    model_version,
                    model_version,
                    MAX_RECORDS + 1,
                ),
            ).fetchall()
    except sqlite3.Error as error:
        raise PredictionLogError("Prediction snapshot is unavailable") from error
    if not rows:
        raise ValueError("No predictions match this scope, version and window")
    if len(rows) > MAX_RECORDS:
        raise ValueError(
            "Snapshot exceeds 20,000 predictions; choose a smaller time window"
        )
    records = []
    for row in rows:
        if row["payload"] is None:
            raise PredictionLogError("Prediction record exceeds the byte limit")
        raw = bytes(row["payload"])
        if sha256(raw).hexdigest() != row["digest"]:
            raise PredictionLogError("Prediction record integrity check failed")
        try:
            event = PredictionEvent.model_validate_json(raw)
        except ValueError as error:
            raise PredictionLogError("Stored prediction schema is invalid") from error
        for name in (
            "observation_id",
            "tenant_id",
            "project_id",
            "model_id",
            "model_version",
        ):
            if getattr(event, name) != row[name]:
                raise PredictionLogError(
                    "Prediction record scope differs from its index"
                )
        if utc_text(event.observed_at) != row["observed_at"]:
            raise PredictionLogError("Prediction timestamp differs from its index")
        records.append(event)
    versions = {event.model_version for event in records}
    if len(versions) != 1:
        raise ValueError(
            "Window contains multiple model versions; select --model-version"
        )
    selected_version = next(iter(versions))
    contents = b"".join(
        event.model_dump_json().encode("utf-8") + b"\n" for event in records
    )
    if len(contents) > MAX_EXPORT_BYTES:
        raise ValueError(
            "Snapshot exceeds the JSONL byte limit; choose a smaller window"
        )
    scope_data = scope.model_dump()
    manifest = {
        "project_id": scope.project_id,
        "model_id": scope.model_id,
        "model_version": selected_version,
        "task_type": "binary_classification",
        "evaluator_id": "metric-policy-v2",
        "policy_version": "serving-quality-example-v1",
        "min_samples": 100,
        "metric_policy": {
            "schema_version": "2",
            "gates": [
                {"metric_id": f"binary.{name}.v1", "operator": "gte", "threshold": 0.8}
                for name in ("precision", "recall", "f1", "balanced_accuracy")
            ],
            "min_positive_samples": 5,
            "min_negative_samples": 30,
            "min_outcome_coverage": 0.5,
        },
    }
    query = {
        **scope_data,
        "model_version": selected_version,
        "dataset_ref": "serving-snapshot-" + str(uuid4()),
        "window_start": start,
        "window_end": end,
        "as_of": cutoff,
    }
    metadata = {
        "mode": "actual_serving_predictions",
        "database": str(database),
        "query": query,
        "prediction_count": len(records),
        "prediction_file_sha256": sha256(contents).hexdigest(),
        "outcome_count": 0,
        "outcomes": "Pending independent confirmation; no labels fabricated",
        "policy": "Illustrative thresholds requiring project-owner calibration",
        "exported_at": datetime.now(UTC).isoformat(),
    }
    documents = {
        "manifest.json": manifest,
        "sources.json": {
            "query": query,
            "predictions_file": "predictions.jsonl",
            "outcomes_file": "outcomes.jsonl",
        },
        "capture-metadata.json": metadata,
    }
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)
    (output_dir / "predictions.jsonl").write_bytes(contents)
    (output_dir / "outcomes.jsonl").write_bytes(b"")
    for name, document in documents.items():
        (output_dir / name).write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    return {**metadata, "output_dir": str(output_dir)}
