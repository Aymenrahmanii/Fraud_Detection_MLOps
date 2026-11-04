"""Scheduled, file-free monitoring through the real Agent 6 reference task handler."""

import asyncio
import json
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.enums import AgentId, Environment, ResultStatus
from agent6_mlops_lifecycle.contracts.mlops import DistributionComparison
from agent6_mlops_lifecycle.contracts.mlops_ingestion import ObservationIngestionRequest
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import MetricEvaluationPolicy, MetricGate
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectEvaluationReport, ProjectManifest
from agent6_mlops_lifecycle.contracts.mlops_storage import project_policy_fingerprint
from agent6_mlops_lifecycle.contracts.tasks import AgentTask, TaskContext
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.fraud.models import FraudSettings
from agent6_mlops_lifecycle.fraud.service import assess_fraud
from agent6_mlops_lifecycle.health.metrics import population_stability_index
from agent6_mlops_lifecycle.ingestion.service import ObservationIngestionService
from agent6_mlops_lifecycle.interfaces.observations import ObservationSnapshot
from agent6_mlops_lifecycle.monitoring.autonomy import automatic_reference, service_probes
from agent6_mlops_lifecycle.monitoring.business_models import OutcomeFeedStatus
from agent6_mlops_lifecycle.monitoring.models import (
    DriftBaseline,
    MonitoringReport,
    PredictionDrift,
    WorkerStatus,
)
from agent6_mlops_lifecycle.monitoring.operations import feature_shift, health, read_operations
from agent6_mlops_lifecycle.monitoring.outcome_feed import poll_outcomes
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    SourceFrame,
    SourceUnavailableError,
    canonical,
)
from agent6_mlops_lifecycle.monitoring.state import MonitorState, MonitorStateError
from agent6_mlops_lifecycle.monitoring.telemetry_models import OperationalHealth, ServiceProbe
from agent6_mlops_lifecycle.service import MLOpsLifecycleAgent

logger = logging.getLogger(__name__)


class WorkerLeaseLostError(MonitorStateError):
    pass


@dataclass(frozen=True)
class _SnapshotSource:
    snapshot: ObservationSnapshot

    def read_snapshot(self) -> ObservationSnapshot:
        return self.snapshot


class MonitoringEngine:
    def __init__(
        self,
        settings: MonitoringSettings,
        source: DirectObservationSource,
        state: MonitorState,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings, self.source, self.state = settings, source, state
        self.fraud_policy = FraudSettings()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.owner = str(uuid4())
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.evidence = SQLiteProjectStore(settings.evidence_database)
        self.service = ProjectEvaluationService(
            manifests=self.evidence,
            batches=self.evidence,
            tenant_id=settings.tenant_id,
            clock=self.clock,
        )
        self.agent = MLOpsLifecycleAgent(project_evaluation=self.service)
        self.ingestion = ObservationIngestionService(
            manifests=self.evidence, batches=self.evidence, tenant_id=settings.tenant_id
        )
        self.last_success: datetime | None = None
        self.operations: dict[str, OperationalHealth] = {}
        self.services: dict[str, ServiceProbe] = {}
        self.outcome_feed = OutcomeFeedStatus(state="NOT_CONFIGURED")

    def window(self) -> tuple[datetime, datetime]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Monitor clock must be timezone-aware")
        end = min(
            now,
            datetime.fromtimestamp(
                math.floor(now.timestamp() / self.settings.interval_seconds)
                * self.settings.interval_seconds,
                UTC,
            ),
        )
        return end - timedelta(seconds=self.settings.quality_window_seconds), end

    def manifest(self, version: str) -> ProjectManifest:
        return ProjectManifest(
            project_id=self.settings.project_id,
            model_id=self.settings.model_id,
            model_version=version,
            task_type="binary_classification",
            evaluator_id="metric-policy-v2",
            policy_version="live-metric-policy-v1",
            min_samples=self.settings.min_samples,
            metric_policy=MetricEvaluationPolicy(
                gates=tuple(
                    MetricGate(
                        metric_id=f"binary.{name}.v1",
                        operator="gte",
                        threshold=self.settings.min_quality_score,
                    )
                    for name in ("precision", "recall", "f1", "balanced_accuracy")
                ),
                min_positive_samples=self.settings.min_positive_samples,
                min_negative_samples=self.settings.min_negative_samples,
                min_outcome_coverage=self.settings.min_outcome_coverage,
            ),
        )

    def drift(
        self, frame: SourceFrame, counts: tuple[int, int], baseline: DriftBaseline | None
    ) -> PredictionDrift:
        common = dict(
            threshold=self.settings.psi_threshold,
            current_samples=sum(counts),
            reference_samples=sum(baseline.prediction_counts) if baseline else 0,
            baseline_sha256=sha256(canonical(baseline)).hexdigest() if baseline else None,
        )
        if baseline is None:
            return PredictionDrift.model_validate({**common, "status": "NO_BASELINE"})
        if baseline.scope != self.settings.scope() or baseline.model_version != frame.model_version:
            raise MonitorStateError("Drift baseline differs from the current scope/model")
        if (
            min(sum(counts), sum(baseline.prediction_counts))
            < self.settings.min_distribution_samples
        ):
            return PredictionDrift.model_validate({**common, "status": "INSUFFICIENT_EVIDENCE"})
        score = population_stability_index(
            DistributionComparison(
                name="predicted_class",
                scope="prediction",
                bins=("0", "1"),
                reference_counts=baseline.prediction_counts,
                current_counts=counts,
            )
        )
        return PredictionDrift.model_validate(
            {
                **common,
                "score": score,
                "status": "DETECTED" if score >= self.settings.psi_threshold else "STABLE",
            }
        )

    async def cycle(self) -> None:
        now = self.clock()
        acquired = await asyncio.to_thread(
            self.state.acquire, self.owner, now, max(60.0, self.settings.interval_seconds * 5)
        )
        if not acquired:
            raise WorkerLeaseLostError("Another worker owns this monitor database")
        self.services = await service_probes(self.settings)
        await asyncio.to_thread(self.state.save_services, self.services, self.clock())
        self.outcome_feed = await asyncio.to_thread(
            poll_outcomes, self.settings, self.source, self.state
        )
        start, end = self.window()
        frames = await asyncio.to_thread(self.source.read_frames, start, end, end)
        operation_start = end - timedelta(seconds=self.settings.window_seconds)
        events = await asyncio.to_thread(read_operations, self.source, operation_start, end)
        versions = {frame.model_version for frame in frames} | {
            event.model_version for event in events
        }
        if len(versions) > 8:
            raise SourceUnavailableError("Serving window exceeds eight model versions")
        self.operations = {}
        for version in sorted(versions):
            selected = tuple(event for event in events if event.model_version == version)
            tracked = sum(
                row.observed_at >= operation_start
                for frame in frames
                if frame.model_version == version
                for row in frame.snapshot.predictions
            )
            assessment = health(selected, version, operation_start, end, tracked, self.settings)
            self.operations[version] = assessment
            await asyncio.to_thread(self.state.save_operational, assessment, self.clock())
        for frame in frames:
            await self._assess(frame, start, end)
        self.last_success = self.clock()
        await asyncio.to_thread(
            self.state.save_status,
            WorkerStatus(
                state="MONITORING" if frames else "WAITING_FOR_PREDICTIONS",
                polled_at=self.last_success,
                last_success_at=self.last_success,
                active_versions=tuple(frame.model_version for frame in frames),
                operations=self.operations,
                outcome_feed=self.outcome_feed,
                services=self.services,
            ),
        )

    async def _assess(self, frame: SourceFrame, start: datetime, end: datetime) -> None:
        if not await asyncio.to_thread(
            self.state.acquire,
            self.owner,
            self.clock(),
            max(60.0, self.settings.interval_seconds * 5),
        ):
            raise WorkerLeaseLostError("Worker lease was lost")
        manifest = self.manifest(frame.model_version)
        baseline = await asyncio.to_thread(
            automatic_reference, self.source, self.state, self.settings, frame.model_version, end
        )
        operation_start = end - timedelta(seconds=self.settings.window_seconds)
        operations = await asyncio.to_thread(
            read_operations, self.source, operation_start, end, frame.model_version
        )
        operational_health = self.operations.get(frame.model_version)
        current_predictions = tuple(
            row
            for row in frame.snapshot.predictions
            if row.observed_at >= operation_start
            and (
                baseline is None
                or baseline.origin != "AUTO_WARMUP"
                or row.observed_at >= baseline.window_end
            )
        )
        comparison_operations = tuple(
            row
            for row in operations
            if baseline is None
            or baseline.origin != "AUTO_WARMUP"
            or row.observed_at >= baseline.window_end
        )
        signature = sha256(
            json.dumps(
                {
                    "scope": self.settings.scope().model_dump(),
                    "model_version": frame.model_version,
                    "predictions": frame.snapshot.prediction_source_ref,
                    "outcomes": frame.snapshot.outcome_source_ref,
                    "operations": sha256(
                        b"".join(canonical(row) for row in operations)
                    ).hexdigest(),
                    "current_prediction_ids": [row.observation_id for row in current_predictions],
                    "operational_policy": [
                        self.settings.min_operational_samples,
                        self.settings.max_failure_rate,
                        self.settings.max_input_issue_rate,
                        self.settings.max_latency_p95_ms,
                    ],
                    "baseline": sha256(canonical(baseline)).hexdigest() if baseline else None,
                    "policy": project_policy_fingerprint(manifest),
                    "fraud_policy": self.fraud_policy.model_dump(mode="json"),
                    "drift_policy": [
                        self.settings.psi_threshold,
                        self.settings.min_distribution_samples,
                    ],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        previous = await asyncio.to_thread(self.state.latest, frame.model_version)
        if previous and previous.data_signature == signature:
            return
        await asyncio.to_thread(self.service.register_project, manifest)
        query = ObservationIngestionRequest(
            tenant_id=self.settings.tenant_id,
            project_id=self.settings.project_id,
            model_id=self.settings.model_id,
            model_version=frame.model_version,
            dataset_ref="live-window-" + signature,
            window_start=start,
            window_end=end,
            as_of=end,
        )
        receipt = await asyncio.to_thread(
            self.ingestion.ingest, query, _SnapshotSource(frame.snapshot)
        )
        evaluation = None
        if receipt.reference is not None:
            reference = receipt.reference
            result = await self.agent.handle(
                AgentTask(
                    task_id="monitor-" + signature[:32],
                    incident_id="live-monitor-" + signature[:24],
                    agent_id=AgentId.MLOPS_LIFECYCLE,
                    objective="Evaluate the current serving window",
                    deadline=datetime.now(UTC) + timedelta(minutes=5),
                    task_context=TaskContext(
                        service_id=self.settings.project_id,
                        namespace="local-monitor",
                        environment=Environment.TEST,
                        signals={"project_evaluation_ref": reference.model_dump(mode="json")},
                        evidence_refs=(reference.evidence_ref,),
                    ),
                )
            )
            if result.status != ResultStatus.SUCCEEDED:
                raise MonitorStateError("Agent 6 evaluation task failed")
            evaluation = ProjectEvaluationReport.model_validate(result.payload)
        counts = (
            sum(row.predicted == 0 for row in frame.snapshot.predictions),
            sum(row.predicted == 1 for row in frame.snapshot.predictions),
        )
        drift_counts = (
            sum(row.predicted == 0 for row in current_predictions),
            sum(row.predicted == 1 for row in current_predictions),
        )
        drift = self.drift(frame, drift_counts, baseline)
        features = feature_shift(comparison_operations, baseline, self.settings)
        fraud = await asyncio.to_thread(assess_fraud, self.source, frame, self.fraud_policy)
        recommendation = (
            "REVIEW_OPERATIONS"
            if operational_health and operational_health.status == "DEGRADED"
            else "REVIEW_MODEL"
            if (evaluation is not None and evaluation.decision == "FAIL") or fraud.status == "FAIL"
            else "REVIEW_SHIFT"
            if drift.status == "DETECTED" or features.status == "DETECTED"
            else "COLLECT_LABELS"
            if evaluation is None or evaluation.decision == "INSUFFICIENT_EVIDENCE"
            else "CONTINUE_MONITORING"
        )
        report = MonitoringReport.model_validate(
            {
                "report_id": sha256((signature + end.isoformat()).encode()).hexdigest(),
                "data_signature": signature,
                "scope": self.settings.scope(),
                "model_version": frame.model_version,
                "assessed_at": self.clock(),
                "prediction_counts": counts,
                "simulated_outcomes": sum(kind == "SIMULATED" for kind in frame.kinds.values()),
                "human_outcomes": sum(kind == "HUMAN" for kind in frame.kinds.values()),
                "system_outcomes": sum(kind == "SYSTEM" for kind in frame.kinds.values()),
                "feature_drift": features,
                "operational_health": operational_health,
                "ingestion": receipt,
                "evaluation": evaluation,
                "fraud_evaluation": fraud,
                "task_status": "SUCCEEDED" if evaluation else "NOT_RUN",
                "drift": drift,
                "recommendation": recommendation,
            }
        )
        await asyncio.to_thread(self.state.save_report, report, self.owner, self.clock())

    async def run(self) -> None:
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                try:
                    await self.cycle()
                except WorkerLeaseLostError:
                    raise
                except (
                    SourceUnavailableError,
                    MonitorStateError,
                    ValueError,
                    RuntimeError,
                ) as error:
                    message = (
                        str(error)
                        if isinstance(error, (SourceUnavailableError, MonitorStateError))
                        else "Monitoring cycle failed; inspect local diagnostics"
                    )
                    logger.error("Monitoring cycle failed (%s)", type(error).__name__)
                    await asyncio.to_thread(
                        self.state.save_status,
                        WorkerStatus(
                            state="WAITING_FOR_SOURCE"
                            if not self.source.database.is_file()
                            else "ERROR",
                            polled_at=self.clock(),
                            last_success_at=self.last_success,
                            error=message,
                            services=self.services,
                            operations=self.operations,
                            outcome_feed=self.outcome_feed,
                        ),
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=self.settings.interval_seconds)
        finally:
            if await asyncio.to_thread(self.state.acquire, self.owner, self.clock(), 10.0):
                await asyncio.to_thread(
                    self.state.save_status,
                    WorkerStatus(
                        state="STOPPED", polled_at=self.clock(), last_success_at=self.last_success
                    ),
                )
                await asyncio.to_thread(self.state.release, self.owner)

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
