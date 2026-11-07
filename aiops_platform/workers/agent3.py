"""Agent 3 (Security & Quality) worker: the release gate.

Objective `assess_release`. Either CI already ran Agent 3 (the usual path:
`.github/workflows/agent3-release-gate.yml` uploads `release_assessment.json`)
and the task carries that assessment, or the worker runs Agent 3's
`run_assessment` itself. Agent 3's deterministic decision is authoritative;
the worker only maps it to the v1 `ReleaseAssessment` payload and stores the
full report as evidence.
"""

import asyncio
from datetime import UTC, datetime

from agent3_security_quality.core.assess import run_assessment
from agent3_security_quality.core.schemas import ReleaseAssessment as A3Assessment
from aiops_platform.contracts.enums import AgentId, ResultStatus
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.payloads import ReleaseAssessment
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.workers.base import AgentWorker, WorkerOutput


class SecurityQualityWorker(AgentWorker):
    agent_id = AgentId.SECURITY_QUALITY

    def __init__(self, bus, context, *, allow_local_scan: bool = False, **kwargs) -> None:
        super().__init__(bus, context, **kwargs)
        self._allow_local_scan = allow_local_scan

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        signals = task.task_context.signals
        if "assessment" in signals:
            assessment = A3Assessment.model_validate(signals["assessment"])
        elif self._allow_local_scan:
            assessment = await asyncio.to_thread(
                run_assessment,
                image_ref=str(signals.get("image_ref", "")),
                strict=bool(signals.get("strict", True)),
            )
        else:
            return WorkerOutput(result=self.failed(task, "no CI assessment supplied and local scans are disabled"))

        evidence = await self.context.append_evidence(
            incident_id=task.incident_id,
            evidence_type="release_assessment",
            summary=f"Agent 3 {assessment.decision} for {assessment.commit[:12]}",
            content=assessment.model_dump(mode="json"),
            observed_at=datetime.now(UTC),
        )
        payload = ReleaseAssessment(
            assessment_id=assessment.assessment_id,
            commit=str(signals.get("commit") or assessment.commit),
            artifact_digest=assessment.artifact.digest or assessment.artifact.image or "unknown",
            decision=assessment.decision,
            blocking_reasons=tuple(assessment.blocking_reasons),
            warnings=tuple(assessment.warnings),
            evidence_refs=(evidence.evidence_id,),
            policy_version=assessment.policy_version,
        ).model_dump(mode="json")
        summary = {
            "tests": assessment.tests.model_dump(exclude={"findings"}),
            "quality_gate": assessment.quality.gate,
            "security": assessment.security.model_dump(exclude={"findings"}),
        }
        result = AgentResult(
            result_id=f"RES-{task.task_id}",
            task_id=task.task_id,
            incident_id=task.incident_id,
            agent_id=self.agent_id,
            status=ResultStatus.SUCCEEDED,
            rationale_summary=assessment.rationale
            or f"{assessment.decision}: {'; '.join(assessment.blocking_reasons or assessment.warnings) or 'all gates passed'}",
            evidence_refs=(evidence.evidence_id,),
            confidence=1.0,  # deterministic scan/test results are authoritative
            payload={**payload, "summary": summary},
        )
        return WorkerOutput(
            result=result,
            events=[("quality.release.assessed", payload, 1.0, (evidence.evidence_id,))],
        )
