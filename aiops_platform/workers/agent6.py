"""Agent 6 (MLOps & Model Lifecycle) worker.

Objectives:

* `assess_model_health`: Agent 6's rule-based assessor over a prediction/label
  window (PSI drift + F1 drop). The window comes from the task's
  `model_health` input (Agent 6's own `ModelHealthInput`), or from a
  configured observation source (Agent 6's local file repositories).
* `retrain_and_evaluate`: Agent 6's `assess_and_train` with its own sklearn
  training pipeline and MLflow registry: candidate vs champion under the
  training policy, registered for review only if it qualifies.

Promotion to serving is not done here: Agent 6 recommends, the Orchestrator
asks a human, and the decision is recorded (see ARCHITECTURE.md).
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from agent6_mlops_lifecycle.adapters.local_files import (
    FileBaselineRepository,
    FileLabelRepository,
    FilePredictionRepository,
    LocalSourceConfig,
)
from agent6_mlops_lifecycle.adapters.mlflow_registry import MLflowModelRegistry
from agent6_mlops_lifecycle.contracts.mlops_training import TrainingPolicy
from agent6_mlops_lifecycle.contracts.tasks import AgentTask as A6Task
from agent6_mlops_lifecycle.service import MLOpsLifecycleAgent
from agent6_mlops_lifecycle.training.demo_data import synthetic_machine_training_dataset
from agent6_mlops_lifecycle.training.pipeline import SklearnTrainingPipeline
from aiops_platform.contracts.enums import AgentId, ResultStatus
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.workers.base import AgentWorker, WorkerOutput

AGENT6_ROOT = Path(__file__).resolve().parents[2] / "agent6_mlops_lifecycle"
DEMO_SOURCE = AGENT6_ROOT / "contracts/examples/agent6/local-sources/machine-sources.json"
DEMO_POLICY = AGENT6_ROOT / "configs/agent6-training-demo.json"


class MLOpsWorker(AgentWorker):
    agent_id = AgentId.MLOPS_LIFECYCLE

    def __init__(
        self,
        bus,
        context,
        *,
        source: Path = DEMO_SOURCE,
        training_policy: Path = DEMO_POLICY,
        output_dir: Path,
        **kwargs,
    ) -> None:
        super().__init__(bus, context, **kwargs)
        self._source = LocalSourceConfig.model_validate_json(source.read_text(encoding="utf-8"))
        self._policy = TrainingPolicy.model_validate_json(training_policy.read_text(encoding="utf-8"))
        output_dir.mkdir(parents=True, exist_ok=True)
        self._agent = MLOpsLifecycleAgent(
            baselines=FileBaselineRepository(source.parent / self._source.baseline_file),
            predictions=FilePredictionRepository(source.parent / self._source.predictions_file),
            labels=FileLabelRepository(source.parent / self._source.labels_file),
            # Both adapters use <dir>/mlflow.db: the registry must see the logged model.
            training=SklearnTrainingPipeline(output_dir / "mlflow"),
            registry=MLflowModelRegistry(output_dir / "mlflow"),
        )

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        if task.objective == "retrain_and_evaluate":
            return await self._retrain(task)
        return await self._assess(task)

    async def _assess(self, task: AgentTask) -> WorkerOutput:
        signals = task.task_context.signals
        if "model_health" in signals:
            a6 = await self._agent.handle(A6Task.model_validate(task.model_dump(mode="json")))
            if a6.status.value != "SUCCEEDED":
                return WorkerOutput(result=AgentResult.model_validate(a6.model_dump(mode="json")))
            assessment, summary = a6.payload, a6.rationale_summary
        else:
            report = await self._agent.assess_model_window(self._source.query)
            assessment, summary = report.assessment.model_dump(mode="json"), report.rationale_summary
            evidence = await self.context.append_evidence(
                incident_id=task.incident_id,
                evidence_type="model_health_report",
                summary=summary[:500],
                content=report.model_dump(mode="json"),
                observed_at=datetime.now(UTC),
            )
            assessment = {**assessment, "evidence_refs": [*assessment["evidence_refs"], evidence.evidence_id]}
        result = AgentResult(
            result_id=f"RES-{task.task_id}",
            task_id=task.task_id,
            incident_id=task.incident_id,
            agent_id=self.agent_id,
            status=ResultStatus.SUCCEEDED,
            rationale_summary=summary,
            evidence_refs=tuple(assessment["evidence_refs"]),
            confidence=None,
            payload=assessment,
        )
        return WorkerOutput(
            result=result,
            events=[("mlops.model.health.assessed", assessment, None, tuple(assessment["evidence_refs"]))],
        )

    async def _retrain(self, task: AgentTask) -> WorkerOutput:
        # Training is CPU-bound (sklearn): run it off the event loop.
        report = await asyncio.to_thread(
            lambda: asyncio.run(
                self._agent.assess_and_train(self._source.query, synthetic_machine_training_dataset(), self._policy)
            )
        )
        evaluation = report.evaluation
        payload = {
            "decision": report.decision,
            "model_name": self._source.query.model_name,
            "registered_model_version": report.registered_model_version,
            "health_decision": report.health.assessment.decision,
            "rationale_summary": report.rationale_summary,
            "limitations": list(report.limitations),
        }
        if evaluation is not None:
            payload.update(
                {
                    "run_id": evaluation.run_id,
                    "candidate_model_uri": evaluation.candidate_model_uri,
                    "candidate": evaluation.candidate.model_dump(mode="json"),
                    "champion": evaluation.champion.model_dump(mode="json"),
                    "policy": {
                        "min_f1_gain": self._policy.min_f1_gain,
                        "max_p95_latency_ms": self._policy.max_p95_latency_ms,
                    },
                }
            )
        evidence = await self.context.append_evidence(
            incident_id=task.incident_id,
            evidence_type="model_lifecycle_report",
            summary=f"{report.decision}: {report.rationale_summary}"[:500],
            content=report.model_dump(mode="json"),
            observed_at=datetime.now(UTC),
        )
        return WorkerOutput(
            result=AgentResult(
                result_id=f"RES-{task.task_id}",
                task_id=task.task_id,
                incident_id=task.incident_id,
                agent_id=self.agent_id,
                status=ResultStatus.SUCCEEDED,
                rationale_summary=report.rationale_summary,
                evidence_refs=(evidence.evidence_id,),
                payload=payload,
            )
        )
