"""Agent 5 (Resource Optimization) worker.

Objective `recommend_resources`. Agent 5's deterministic optimizer evaluates one
sustained metric window. Metrics come from Agent 5's own collectors (live) or
from the simulated cluster; the task's signals are only a fallback. A
`NO_CHANGE` recommendation is reported as a task result, not as a candidate action.
"""

from collections.abc import Callable

from pydantic import JsonValue

from agent5_resource_optimization.core.optimizer import build_recommendation
from agent5_resource_optimization.core.schemas import ResourceMetrics, ServiceRef
from aiops_platform.contracts.actions import ActionIntent, ActionTarget
from aiops_platform.contracts.enums import ActionType, AgentId, ResultStatus
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.payloads import ResourceRecommendation
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.workers.base import AgentWorker, WorkerOutput

MetricsSource = Callable[[str, str], ResourceMetrics]


def metrics_from_signals(signals: dict[str, JsonValue]) -> ResourceMetrics:
    def number(*keys: str) -> float | None:
        for key in keys:
            value = signals.get(key)
            if isinstance(value, int | float):
                return float(value)
        return None

    replicas = int(number("replicas") or 1)
    error = number("error_rate")
    return ResourceMetrics(
        replicas=replicas,
        ready_replicas=int(number("ready_replicas") or replicas),
        cpu_avg_pct=number("cpu_avg_pct", "cpu_percent") or 0.0,
        memory_avg_pct=number("memory_pct", "memory_percent", "memory_avg_pct") or 0.0,
        latency_p95_ms=number("latency_p95_ms", "latency_ms"),
        rps=number("rps"),
        error_rate_pct=None if error is None else min(100.0, error * 100 if error <= 1 else error),
        memory_limit_mib=number("memory_limit_mib"),
    )


def cluster_metrics(cluster) -> MetricsSource:
    def read(service_id: str, namespace: str) -> ResourceMetrics:
        return metrics_from_signals(dict(cluster.signals(service_id, namespace)))

    return read


class ResourceOptimizationWorker(AgentWorker):
    agent_id = AgentId.RESOURCE_OPTIMIZATION

    def __init__(self, bus, context, *, metrics: MetricsSource | None = None, **kwargs) -> None:
        super().__init__(bus, context, **kwargs)
        self._metrics = metrics

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        ctx = task.task_context
        metrics = (
            self._metrics(ctx.service_id, ctx.namespace)
            if self._metrics
            else metrics_from_signals(dict(ctx.signals))
        )
        rec = build_recommendation(
            metrics,
            service=ServiceRef(service_id=ctx.service_id, namespace=ctx.namespace, version=request.service.version),
            environment=ctx.environment.value,
            correlation_id=task.incident_id,
            causation_id=request.message_id,
            trace_id=request.trace_id,
        )
        body = rec.payload
        action = body.recommendation
        result_payload = rec.model_dump(mode="json", by_alias=True)
        if action.action == "NO_CHANGE":
            return WorkerOutput(
                result=AgentResult(
                    result_id=f"RES-{task.task_id}",
                    task_id=task.task_id,
                    incident_id=task.incident_id,
                    agent_id=self.agent_id,
                    status=ResultStatus.SUCCEEDED,
                    rationale_summary="NO_CHANGE: " + "; ".join(body.reasons),
                    confidence=rec.confidence,
                    payload=result_payload,
                )
            )
        if action.action == "SCALE_REPLICAS":
            kind, parameters = ActionType.SCALE_REPLICAS, {"from": action.from_replicas, "to": action.to_replicas, "replicas": action.to_replicas}
        else:
            kind, parameters = ActionType.INCREASE_MEMORY, {
                "memory_limit_from_mib": int(action.memory_limit_from_mib or 0),
                "memory_limit_to_mib": int(action.memory_limit_to_mib or 0),
            }
        intent = ActionIntent(
            action_id=f"ACTION-{task.task_id}-1",
            incident_id=task.incident_id,
            action=kind,
            environment=ctx.environment,
            target=ActionTarget(service=ctx.service_id, namespace=ctx.namespace),
            parameters=parameters,
            verification_profile=f"{ctx.service_id}-slo-default",
        )
        payload = ResourceRecommendation(
            recommendation_id=body.recommendation_id,
            candidate_action=intent,
            confidence=rec.confidence,
            observation_window=body.window,
            rollback_condition=body.rollback_condition,
            constraints=tuple(body.constraints),
        ).model_dump(mode="json")
        extra = {
            "current": body.current.model_dump(mode="json"),
            "expected_effect": body.expected_effect.model_dump(mode="json"),
            "reasons": list(body.reasons),
        }
        return WorkerOutput(
            result=AgentResult(
                result_id=f"RES-{task.task_id}",
                task_id=task.task_id,
                incident_id=task.incident_id,
                agent_id=self.agent_id,
                status=ResultStatus.SUCCEEDED,
                rationale_summary="; ".join(body.reasons),
                confidence=rec.confidence,
                payload={**payload, **extra},
            ),
            events=[("resource.recommendation.created", payload, rec.confidence, ())],
        )
