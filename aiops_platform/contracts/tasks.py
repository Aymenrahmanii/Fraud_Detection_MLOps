from typing import Annotated

from pydantic import AwareDatetime, Field, JsonValue

from aiops_platform.contracts.base import Contract
from aiops_platform.contracts.enums import AgentId, Environment, ResultStatus


class TaskContext(Contract):
    """Bounded disclosure supplied by the orchestrator, never a full context snapshot."""

    service_id: Annotated[str, Field(min_length=1)]
    namespace: Annotated[str, Field(min_length=1)]
    environment: Environment
    signals: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


class AgentTask(Contract):
    task_id: Annotated[str, Field(min_length=1)]
    incident_id: Annotated[str, Field(min_length=1)]
    agent_id: AgentId
    objective: Annotated[str, Field(min_length=1)]
    task_context: TaskContext
    deadline: AwareDatetime


class AgentResult(Contract):
    """Execution status plus the agent's typed domain output.

    `payload` follows Agent 6's contract: the specialist output (for example a
    `DiagnosisCompleted`) as JSON, so the Orchestrator can read typed values
    instead of prose. The worker that wraps each agent fills it.
    """

    result_id: Annotated[str, Field(min_length=1)]
    task_id: Annotated[str, Field(min_length=1)]
    incident_id: Annotated[str, Field(min_length=1)]
    agent_id: AgentId
    status: ResultStatus
    rationale_summary: str
    evidence_refs: tuple[str, ...] = ()
    confidence: Annotated[float | None, Field(ge=0, le=1, allow_inf_nan=False)] = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
