"""Agent 4 (Deployment & Recovery) worker on the bus.

* task `prepare_action`: build and dry-run the exact plan, publish
  `deployment.plan.prepared` (no side effect);
* `workflow.continuation` with `next_step=EXECUTE_ACTION` (after HITL) or
  `deployment.action.requested` (AUTO): execute the prepared plan through
  Agent 4's coordinator with the permit, publish `deployment.action.completed`.

Plans the worker prepared are kept by (action_id, version): execution only runs
a plan this worker prepared, and only if its hashes match the signed permit.
Agent 4's own SQLite journal makes a redelivered execution resume, not repeat.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from aiops_platform.bus import ReliableConsumer, publish
from aiops_platform.contracts.actions import ActionRequest
from aiops_platform.contracts.enums import AgentId, PolicyDecision, ResultStatus
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.payloads import ActionCompleted, WorkflowContinuation
from aiops_platform.contracts.platform_payloads import PlanPrepared
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.contracts.topics import PLAN_PREPARED
from aiops_platform.hitl.service import PROVIDER_ID, decode_token
from aiops_platform.workers.agent4_executor import Agent4Executor
from aiops_platform.workers.base import AgentWorker, WorkerOutput, reply

log = logging.getLogger(__name__)


class PlanStore:
    """Prepared plans by action; optionally persisted so a restart can still execute."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self._plans: dict[tuple[str, int], PlanPrepared] = {}
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def put(self, plan: PlanPrepared) -> None:
        self._plans[(plan.action_id, plan.action_version)] = plan
        if self._dir:
            (self._dir / f"{plan.action_id}-v{plan.action_version}.json").write_text(plan.model_dump_json())

    def get(self, action_id: str, version: int) -> PlanPrepared | None:
        plan = self._plans.get((action_id, version))
        if plan is None and self._dir:
            path = self._dir / f"{action_id}-v{version}.json"
            if path.exists():
                plan = PlanPrepared.model_validate_json(path.read_text())
                self._plans[(action_id, version)] = plan
        return plan


class DeploymentWorker(AgentWorker):
    agent_id = AgentId.DEPLOYMENT_RECOVERY

    def __init__(self, bus, context, *, executor: Agent4Executor, plans: PlanStore | None = None, **kwargs) -> None:
        super().__init__(bus, context, **kwargs)
        self._executor = executor
        self._plans = plans or PlanStore()

    def subscribe(self) -> None:
        super().subscribe()
        consumer = ReliableConsumer(
            self.bus,
            "worker.deployment-recovery-agent.actions",
            self._on_action,
            idempotency=self._idempotency,  # Redis in compose: dedup survives restarts
            observer=self.metrics.observer(),
        )
        consumer.subscribe(["workflow.continuation", "deployment.action.requested"])

    # ---- prepare -------------------------------------------------------

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        if task.objective != "prepare_action":
            return WorkerOutput(result=self.failed(task, f"unsupported objective {task.objective}"))
        s = task.task_context.signals
        plan = await asyncio.to_thread(
            self._executor.prepare,
            incident_id=task.incident_id,
            trace_id=request.trace_id,
            environment=task.task_context.environment.value,
            service_id=task.task_context.service_id,
            namespace=task.task_context.namespace,
            action_id=str(s["action_id"]),
            action_version=int(s.get("action_version", 1)),
            action=str(s["action"]),
            parameters=dict(s.get("parameters") or {}),
        )
        if plan.status == "PREPARED":
            self._plans.put(plan)
        payload = plan.model_dump(mode="json")
        result = AgentResult(
            result_id=f"RES-{task.task_id}",
            task_id=task.task_id,
            incident_id=task.incident_id,
            agent_id=self.agent_id,
            status=ResultStatus.SUCCEEDED,
            rationale_summary=f"{plan.status}: {plan.reason}",
            payload=payload,
        )
        return WorkerOutput(result=result, events=[(PLAN_PREPARED, payload, None, ())])

    # ---- execute -------------------------------------------------------

    async def _on_action(self, envelope: EventEnvelope) -> None:
        if envelope.topic == "workflow.continuation":
            if envelope.target != self.agent_id.value:
                return
            continuation = WorkflowContinuation.model_validate(envelope.payload)
            if continuation.next_step != "EXECUTE_ACTION" or continuation.action_request is None:
                return
            request = continuation.action_request
        else:
            request = ActionRequest.model_validate(envelope.payload)
        intent = request.intent
        if request.policy_decision is PolicyDecision.BLOCK or not request.approval_token:
            await self._complete(envelope, intent.action_id, intent.action_version, "BLOCKED", {}, {}, "no permit")
            return
        plan = self._plans.get(intent.action_id, intent.action_version)
        if plan is None:
            await self._complete(
                envelope, intent.action_id, intent.action_version, "BLOCKED", {}, {}, "no prepared plan"
            )
            return
        claims = decode_token(request.approval_token).claims
        if (claims.plan_hash, claims.patch_hash) != (plan.plan_hash, plan.patch_hash):
            await self._complete(
                envelope,
                intent.action_id,
                intent.action_version,
                "BLOCKED",
                {},
                {},
                "permit does not match the prepared plan",
            )
            return
        receipt = {
            "approval_id": claims.approval.receipt_id,
            "issuer": PROVIDER_ID,
            "approver_subject": claims.approval.approver_subject,
            "approved_at": claims.approval.approved_at.isoformat(),
        }
        started = time.perf_counter()
        outcome = await asyncio.to_thread(
            self._executor.execute, plan, receipt=receipt, approval_token=request.approval_token
        )
        self.metrics.task_done(self.agent_id.value, "execute_action", outcome.status, time.perf_counter() - started)
        evidence = await self.context.append_evidence(
            incident_id=intent.incident_id,
            evidence_type="k8s_action",
            summary=f"{intent.action} {outcome.state}: {outcome.reason}"[:500],
            content={
                "execution_id": outcome.execution_id,
                "state": outcome.state,
                "reason": outcome.reason,
                "pre_state": outcome.pre_state,
                "post_state": outcome.post_state,
                "operator_intervention_required": outcome.operator_intervention_required,
                "audit_chain_valid": self._executor.audit_chain_valid(outcome.execution_id),
            },
            observed_at=datetime.now(UTC),
        )
        await self._complete(
            envelope,
            intent.action_id,
            intent.action_version,
            outcome.status,
            outcome.pre_state,
            outcome.post_state,
            outcome.reason,
            (evidence.evidence_id,),
            outcome.execution_id,
        )

    async def _complete(
        self,
        envelope,
        action_id,
        version,
        status,
        pre,
        post,
        reason,
        evidence=(),
        execution_id=None,
    ) -> None:
        payload = ActionCompleted(
            action_id=action_id,
            action_version=version,
            status=status,
            pre_state={**pre, "execution_id": execution_id, "reason": reason},
            post_state=post,
            execution_evidence_refs=evidence,
            verification_requested=status == "SUCCESS",
        ).model_dump(mode="json")
        result = AgentResult(
            result_id=f"RES-EXEC-{action_id}-v{version}",
            task_id=f"EXEC-{action_id}-v{version}",
            incident_id=envelope.correlation_id,
            agent_id=self.agent_id,
            status=ResultStatus.SUCCEEDED if status == "SUCCESS" else ResultStatus.FAILED,
            rationale_summary=f"{status}: {reason}"[:2000],
            evidence_refs=tuple(evidence),
            payload=payload,
        )
        await self.context.append_agent_result(result, "ACTION_COMPLETED")
        await publish(
            self.bus,
            reply(
                envelope,
                source=self.agent_id.value,
                topic="deployment.action.completed",
                payload=payload,
                evidence_refs=tuple(evidence),
                key_suffix=json.dumps([action_id, version]),
            ),
        )
