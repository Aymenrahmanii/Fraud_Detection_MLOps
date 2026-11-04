"""Asynchronous isolated inference and deterministic evaluation of labeled benchmarks."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from agent6_mlops_lifecycle.assurance.models import (
    BenchmarkDataset,
    BenchmarkRegistry,
    BenchmarkReport,
    ModelArtifact,
)
from agent6_mlops_lifecycle.assurance.store import AssuranceStore, encode
from agent6_mlops_lifecycle.assurance.worker import artifact_digest
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import MetricEvaluationPolicy, MetricGate
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    BinaryEvaluationSample,
    EvaluationBatch,
    ProjectManifest,
)
from agent6_mlops_lifecycle.evaluation.builtin_metrics import default_metric_registry
from agent6_mlops_lifecycle.evaluation.policy_evaluator import MetricPolicyEvaluator
from agent6_mlops_lifecycle.monitoring.settings import ROOT, MonitoringSettings
from agent6_mlops_lifecycle.processes import NO_WINDOW_FLAGS


def policy(settings: MonitoringSettings) -> MetricEvaluationPolicy:
    return MetricEvaluationPolicy(
        gates=tuple(
            MetricGate(
                metric_id=f"binary.{name}.v1", operator="gte", threshold=settings.min_quality_score
            )
            for name in ("precision", "recall", "f1", "balanced_accuracy")
        ),
        min_positive_samples=1,
        min_negative_samples=1,
    )


async def infer(
    settings: MonitoringSettings, registry: BenchmarkRegistry, artifact: ModelArtifact, digest: str
) -> list[int]:
    if settings.benchmark_python is None:
        raise ValueError("Configure a compatible benchmark Python runtime")
    request = {
        "project_root": str(registry.project_root),
        "artifact": str(artifact.path),
        "kind": artifact.kind,
        "artifact_sha256": digest,
        "database": str(settings.benchmark_database),
        "dataset_id": registry.dataset_id,
    }
    # The model runtime receives no API keys or monitor credentials.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PROGRAMFILES",
        }
    }
    env.update(
        {"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "MLFLOW_DISABLE_AGENT_HINT": "1"}
    )
    process = await asyncio.create_subprocess_exec(
        str(settings.benchmark_python),
        "-B",
        "-m",
        "agent6_mlops_lifecycle.assurance.worker",
        cwd=registry.project_root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=NO_WINDOW_FLAGS,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(encode(request)), timeout=settings.benchmark_timeout_seconds
        )
    finally:
        if process.returncode is None:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=NO_WINDOW_FLAGS,
                )
                await killer.wait()
            else:
                process.kill()
            await process.wait()
    if process.returncode != 0 or len(output) > 1_000_000:
        raise RuntimeError("Saved-model benchmark failed; check compatible runtime and artifact")
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("Invalid benchmark response envelope")
    predictions = result.get("predictions")
    if (
        result.get("artifact_sha256") != digest
        or result.get("dataset_id") != registry.dataset_id
        or not isinstance(predictions, list)
        or any(type(value) is not int or value not in (0, 1) for value in predictions)
    ):
        raise ValueError("Benchmark response does not match the pinned evidence")
    return predictions


async def evaluate(
    settings: MonitoringSettings,
    store: AssuranceStore,
    registry: BenchmarkRegistry,
    artifact: ModelArtifact,
    digest: str,
    predictions: list[int],
    now: datetime,
) -> BenchmarkReport:
    dataset = BenchmarkDataset.model_validate(store.get("dataset", registry.dataset_id))
    if len(predictions) != len(dataset.rows):
        raise ValueError("Benchmark predictions and labels must align exactly")
    evidence = store.put(
        "inference",
        {
            "dataset_id": registry.dataset_id,
            "model_version": "artifact-" + digest,
            "predictions": predictions,
        },
    )
    evaluator = MetricPolicyEvaluator(
        "binary_classification", default_metric_registry(), lambda: now
    )
    manifest = ProjectManifest(
        project_id=settings.project_id,
        model_id=settings.model_id,
        model_version="artifact-" + digest,
        task_type="binary_classification",
        evaluator_id="metric-policy-v2",
        policy_version="labeled-benchmark-v1",
        min_samples=2,
        metric_policy=policy(settings),
    )
    evaluations = {}
    label_counts = {}
    for group in ("ALL", *sorted({row.group for row in dataset.rows})):
        samples = tuple(
            BinaryEvaluationSample(
                observation_id=f"benchmark-{index}",
                observed_at=now,
                expected=row.actual,
                predicted=1 if prediction == 1 else 0,
            )
            for index, (row, prediction) in enumerate(zip(dataset.rows, predictions, strict=True))
            if group == "ALL" or row.group == group
        )
        label_counts[group] = (
            sum(row.expected == 0 for row in samples),
            sum(row.expected == 1 for row in samples),
        )
        batch = EvaluationBatch(
            project_id=settings.project_id,
            model_id=settings.model_id,
            model_version=manifest.model_version,
            dataset_ref=registry.dataset_id,
            evidence_ref="sha256:" + evidence,
            samples=samples,
        )
        evaluations[group] = await evaluator.evaluate(manifest, batch)
    return BenchmarkReport(
        model_version=manifest.model_version,
        artifact_name=artifact.name,
        dataset_id=registry.dataset_id,
        assessed_at=now,
        source_sha256=dataset.source_sha256,
        evaluations=evaluations,
        label_counts=label_counts,
    )


async def run_benchmarks(settings: MonitoringSettings, store: AssuranceStore) -> None:
    if settings.benchmark_registry is None:
        return
    path = settings.benchmark_registry
    if path.stat().st_size > 32768:
        raise ValueError("Benchmark registry exceeds its size limit")
    registry = BenchmarkRegistry.model_validate_json(path.read_bytes())
    root = registry.project_root.resolve(strict=True)
    for artifact in registry.models:
        resolved = artifact.path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("Benchmark artifact is outside its trusted project")
        digest = await asyncio.to_thread(artifact_digest, resolved)
        key = sha256(
            encode(
                {
                    "model": digest,
                    "dataset": registry.dataset_id,
                    "policy": policy(settings).model_dump(mode="json"),
                    "version": "labeled-benchmark-v1",
                    "report_format": "benchmark-class-support-v1",
                }
            )
        ).hexdigest()
        now = datetime.now(UTC)
        previous = await asyncio.to_thread(store.job, key)
        if previous and (now - datetime.fromisoformat(previous["updated_at"])).total_seconds() < (
            settings.benchmark_timeout_seconds + 15
            if previous["status"] == "RUNNING"
            else settings.benchmark_interval_seconds
        ):
            continue
        checkpoint: dict[str, Any] = {
            "artifact_name": artifact.name,
            "model_version": "artifact-" + digest,
            "dataset_id": registry.dataset_id,
            "status": "RUNNING",
            "report_ref": previous.get("report_ref") if previous else None,
        }
        await asyncio.to_thread(store.save_job, key, "benchmark", now, checkpoint)
        try:
            predictions = await infer(settings, registry, artifact, digest)
            report = await evaluate(
                settings, store, registry, artifact, digest, predictions, datetime.now(UTC)
            )
            reference = await asyncio.to_thread(
                store.put, "benchmark", report.model_dump(mode="json")
            )
            checkpoint.update(status="COMPLETED", report_ref=reference)
        except (ValueError, RuntimeError, OSError, TimeoutError, KeyError, TypeError):
            checkpoint.update(
                status="ERROR",
                error=(
                    "Benchmark failed; inspect compatible model runtime, "
                    "artifact and dataset configuration"
                ),
            )
        await asyncio.to_thread(store.save_job, key, "benchmark", datetime.now(UTC), checkpoint)
