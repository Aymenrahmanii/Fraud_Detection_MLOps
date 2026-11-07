"""Orchestrator service: bus consumer + LangGraph thread manager + HITL entry point.

* An `ANOMALY_DETECTED` for an unknown incident starts an INCIDENT workflow;
  `start_release()` / `start_model_check()` start the other flows.
* Every result event is routed to its workflow thread by `correlation_id` and
  resumes it; per-incident locks keep one step at a time per workflow.
* `resolve_approval()` is what the Dashboard calls (v5 7: Dashboard ->
  Orchestrator -> HITL Service). It resolves through the HITL Service, records
  the decision and resumes the paused thread.
* `sweep()` turns overdue tasks into timeouts and overdue approvals into
  EXPIRED, so no workflow waits forever.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aiops_platform.bus import EventBus, InMemoryIdempotencyStore, ReliableConsumer
from aiops_platform.bus.idempotency import IdempotencyStore
from aiops_platform.context.client import OrchestratorContext
from aiops_platform.context.service import Principal
from aiops_platform.contracts.enums import Priority, Severity
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.hitl.service import HitlService, Operator
from aiops_platform.observability.metrics import PlatformMetrics
from aiops_platform.orchestrator.graph import Deps, build_graph
from aiops_platform.policy import PolicyGate

log = logging.getLogger(__name__)

RESULT_TOPICS = [
    "monitoring.anomaly.detected",
    "monitoring.recovery.verified",
    "diagnosis.completed",
    "quality.release.assessed",
    "deployment.plan.prepared",
    "deployment.action.completed",
    "resource.recommendation.created",
    "mlops.model.health.assessed",
    "agent.task.result",
]


class HitlClient:
    """HitlService with the Orchestrator principal bound (in-process or gRPC)."""

    def __init__(self, hitl: HitlService) -> None:
        self._hitl = hitl
        self._me = Principal.orchestrator()

    async def create_approval(self, request):
        return await self._hitl.create_approval(self._me, request)

    async def issue_policy_permit(self, request):
        return await self._hitl.issue_policy_permit(self._me, request)

    async def resolve_approval(self, approval_id, **kwargs):
        return await self._hitl.resolve_approval(self._me, approval_id, **kwargs)

    async def get_approval_state(self, approval_id):
        return await self._hitl.get_approval_state(self._me, approval_id)

    async def expire_due(self):
        return await self._hitl.expire_due()


class OrchestratorService:
    def __init__(
        self,
        *,
        bus: EventBus,
        context: OrchestratorContext,
        hitl: HitlClient,
        checkpointer=None,
        policy: PolicyGate | None = None,
        idempotency: IdempotencyStore | None = None,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self.bus = bus
        self.context = context
        self.hitl = hitl
        self.metrics = metrics or PlatformMetrics()
        self.graph = build_graph(
            Deps(bus=bus, context=context, hitl=hitl, policy=policy), checkpointer or InMemorySaver()
        )
        self._idempotency = idempotency or InMemoryIdempotencyStore()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._started: dict[str, datetime] = {}

    def subscribe(self) -> None:
        ReliableConsumer(self.bus, "orchestrator", self.on_event, idempotency=self._idempotency).subscribe(
            RESULT_TOPICS
        )

    @staticmethod
    def _config(incident_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": incident_id}}

    # ---- starting workflows ---------------------------------------------

    async def start(
        self,
        *,
        incident_id: str,
        kind: str,
        service: dict[str, str],
        environment: str,
        severity: str,
        priority: str,
        signals: dict[str, Any],
        trigger: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._locks[incident_id]:
            if await self._exists(incident_id):
                return await self.status(incident_id)
            self._started[incident_id] = datetime.now(UTC)
            self.metrics.workflow_started(kind)
            await self.graph.ainvoke(
                {
                    "incident_id": incident_id,
                    "kind": kind,
                    "environment": environment,
                    "priority": priority,
                    "severity": severity,
                    "service": service,
                    "trace_id": trace_id or f"TRACE-{incident_id}",
                    "trigger": trigger or {"service": service},
                    "signals": signals,
                    "results": {},
                },
                self._config(incident_id),
            )
        await self._after_step(incident_id)
        return await self.status(incident_id)

    async def start_release(
        self,
        *,
        release_id: str,
        service: dict[str, str],
        environment: str,
        version: str,
        commit: str,
        assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        signals: dict[str, Any] = {"version": version, "commit": commit}
        if assessment is not None:
            signals["assessment"] = assessment
        return await self.start(
            incident_id=release_id,
            kind="RELEASE",
            service=service,
            environment=environment,
            severity=Severity.MEDIUM.value,
            priority=Priority.P2.value,
            signals=signals,
        )

    async def start_model_check(
        self, *, check_id: str, service: dict[str, str], environment: str, signals: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.start(
            incident_id=check_id,
            kind="MLOPS",
            service=service,
            environment=environment,
            severity=Severity.MEDIUM.value,
            priority=Priority.P2.value,
            signals=signals or {},
        )

    # ---- events ------------------------------------------------------------

    async def on_event(self, envelope: EventEnvelope) -> None:
        incident_id = envelope.correlation_id
        if (
            envelope.topic == "monitoring.anomaly.detected"
            and envelope.source == "monitoring-agent"
            and not await self._exists(incident_id)
        ):
            await self.start(
                incident_id=incident_id,
                kind="INCIDENT",
                service=envelope.service.model_dump(mode="json"),
                environment=envelope.environment.value,
                severity=envelope.severity.value,
                priority=envelope.priority.value,
                signals={
                    k: v for k, v in envelope.payload.get("signals", {}).items() if not isinstance(v, (list, dict))
                },
                trigger=envelope.model_dump(mode="json"),
                trace_id=envelope.trace_id,
            )
            return
        await self.resume(incident_id, envelope.model_dump(mode="json"))

    async def resume(self, incident_id: str, event: dict[str, Any]) -> None:
        async with self._locks[incident_id]:
            if not await self._waiting(incident_id):
                log.debug("event for %s ignored: workflow not waiting", incident_id)
                return
            await self.graph.ainvoke(Command(resume=event), self._config(incident_id))
        await self._after_step(incident_id)

    # ---- HITL entry point (called by the Dashboard) ------------------------

    async def resolve_approval(
        self, approval_id: str, *, decision: str, operator: Operator, comment: str
    ) -> dict[str, Any]:
        state = await self.hitl.resolve_approval(approval_id, decision=decision, operator=operator, comment=comment)
        self.metrics.hitl_resolved(state.state, (datetime.now(UTC) - state.expires_at).total_seconds())
        await self.resume(
            state.incident_id, {"topic": "hitl", "approval_id": approval_id, "approval": state.model_dump(mode="json")}
        )
        return state.model_dump(mode="json", exclude={"approval_token"})

    # ---- timeouts / expiry ---------------------------------------------------

    async def sweep(self) -> None:
        for approval_id in await self.hitl.expire_due():
            state = await self.hitl.get_approval_state(approval_id)
            await self.resume(
                state.incident_id,
                {"topic": "hitl", "approval_id": approval_id, "approval": state.model_dump(mode="json")},
            )
        now = datetime.now(UTC)
        for incident_id in list(self._started):
            snapshot = await self.graph.aget_state(self._config(incident_id))
            pending = snapshot.values.get("pending", {}) if snapshot.values else {}
            if not snapshot.next or not pending:
                continue
            stage_age = (now - self._started[incident_id]).total_seconds()
            if stage_age < 600:
                continue
            for message_id in list(pending):
                await self.resume(incident_id, {"topic": "timeout", "causation_id": message_id})

    # ---- introspection -------------------------------------------------------

    async def status(self, incident_id: str) -> dict[str, Any]:
        snapshot = await self.graph.aget_state(self._config(incident_id))
        values = dict(snapshot.values or {})
        values.pop("trigger", None)
        approval = values.get("approval")
        if approval:
            values["approval"] = {k: v for k, v in approval.items() if k != "approval_token"}
        values["waiting"] = [i.value for t in snapshot.tasks for i in t.interrupts]
        values["done"] = not snapshot.next
        return values

    async def _exists(self, incident_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(incident_id))
        return bool(snapshot.values)

    async def _waiting(self, incident_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(incident_id))
        return bool(snapshot.next)

    async def _after_step(self, incident_id: str) -> None:
        snapshot = await self.graph.aget_state(self._config(incident_id))
        if not snapshot.next and incident_id in self._started:
            values = snapshot.values or {}
            seconds = (datetime.now(UTC) - self._started.pop(incident_id)).total_seconds()
            self.metrics.workflow_finished(values.get("kind", "?"), values.get("stage", "?"), seconds)
            if values.get("policy"):
                self.metrics.policy_decision(values["policy"]["decision"])
