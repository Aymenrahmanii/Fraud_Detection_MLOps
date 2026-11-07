"""Common agent-worker loop (v5 section 6.1 steps 4-7).

A worker consumes `agent.task.<agent-id>`, runs its agent on the bounded
`task_context`, appends the result (and new evidence) to the Context Service,
then publishes the typed result event. A failed task, or one with nothing to
report, is published on `agent.task.result` so the Orchestrator never waits.

Workers never read the incident context: the task is all they get.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from aiops_platform.bus import EventBus, InMemoryIdempotencyStore, ReliableConsumer, publish
from aiops_platform.bus.idempotency import IdempotencyStore
from aiops_platform.context.client import AgentContextPort
from aiops_platform.contracts.enums import AgentId, ResultStatus
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.contracts.topics import TASK_RESULT, TOPICS, task_topic

log = logging.getLogger(__name__)


@dataclass
class WorkerOutput:
    result: AgentResult
    # (topic, payload, confidence, evidence_refs) of domain events to publish.
    events: list[tuple[str, dict[str, JsonValue], float | None, tuple[str, ...]]] = field(
        default_factory=list
    )


def reply(
    request: EventEnvelope,
    *,
    source: str,
    topic: str,
    payload: dict[str, JsonValue],
    confidence: float | None = None,
    evidence_refs: tuple[str, ...] = (),
    target: str = "orchestrator",
    key_suffix: str = "v1",
) -> EventEnvelope:
    """An event caused by `request`, inheriting its correlation, trace and scope."""
    message_type = TOPICS[topic][0]
    now = datetime.now(UTC)
    deadline = request.deadline if request.deadline and request.deadline > now else None
    return EventEnvelope(
        correlation_id=request.correlation_id,
        causation_id=request.message_id,
        trace_id=request.trace_id,
        source=source,
        target=target,
        topic=topic,
        message_type=message_type,
        priority=request.priority,
        severity=request.severity,
        timestamp=now,
        deadline=deadline,
        environment=request.environment,
        service=request.service,
        evidence_refs=evidence_refs,
        confidence=confidence,
        idempotency_key=f"{request.correlation_id}:{message_type}:{request.message_id}:{key_suffix}",
        payload=payload,
    )


class AgentWorker(ABC):
    agent_id: AgentId

    def __init__(
        self,
        bus: EventBus,
        context: AgentContextPort,
        *,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self.bus = bus
        self.context = context
        self._idempotency = idempotency or InMemoryIdempotencyStore()

    def subscribe(self) -> None:
        consumer = ReliableConsumer(
            self.bus, f"worker.{self.agent_id.value}", self._on_task, idempotency=self._idempotency
        )
        consumer.subscribe([task_topic(self.agent_id)])

    @abstractmethod
    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        """Run the wrapped agent on one task."""

    async def _on_task(self, envelope: EventEnvelope) -> None:
        task = AgentTask.model_validate(envelope.payload)
        if task.agent_id is not self.agent_id:
            log.warning("%s ignored a task for %s", self.agent_id, task.agent_id)
            return
        try:
            output = await self.run(task, envelope)
        except Exception as exc:  # noqa: BLE001 - report the failure; the Orchestrator replans
            log.exception("%s failed task %s", self.agent_id, task.task_id)
            output = WorkerOutput(result=self.failed(task, f"{type(exc).__name__}: {exc}"))
        result = output.result
        succeeded = result.status is ResultStatus.SUCCEEDED
        events = output.events if succeeded else []
        result_type = TOPICS[events[0][0]][0] if events else f"{self.agent_id.value}:{task.objective}"
        # Step 5: append first, so the context is complete when the event arrives.
        await self.context.append_agent_result(result, result_type)
        for topic, payload, confidence, evidence in events:
            await publish(
                self.bus,
                reply(
                    envelope,
                    source=self.agent_id.value,
                    topic=topic,
                    payload=payload,
                    confidence=confidence,
                    evidence_refs=evidence,
                ),
            )
        if not events:
            # Failure, or success with nothing to report: the Orchestrator must not wait.
            await publish(
                self.bus,
                reply(
                    envelope,
                    source=self.agent_id.value,
                    topic=TASK_RESULT,
                    payload=result.model_dump(mode="json"),
                    confidence=result.confidence,
                    key_suffix="result",
                ),
            )

    def failed(self, task: AgentTask, reason: str) -> AgentResult:
        return AgentResult(
            result_id=f"RES-{task.task_id}",
            task_id=task.task_id,
            incident_id=task.incident_id,
            agent_id=self.agent_id,
            status=ResultStatus.FAILED,
            rationale_summary=reason[:2000],
        )


def default_deadline(minutes: int = 5) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)
