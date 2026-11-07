"""Context Service records (v5 section 6).

`EvidenceReference` and the first five `IncidentContext` fields are exactly the
Agent 1/2 shared contract. The remaining fields are the v5 section 6.3 record
the Orchestrator reads; they have defaults, so older payloads still validate.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from aiops_platform.contracts.base import Contract
from aiops_platform.contracts.enums import Environment, PolicyDecision, Priority, Severity
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

WorkflowKind = Literal["INCIDENT", "RELEASE", "MLOPS"]
HitlState = Literal["NOT_REQUESTED", "WAITING_FOR_HUMAN", "APPROVED", "REJECTED", "EXPIRED"]


class EvidenceReference(Contract):
    evidence_id: str
    incident_id: str
    source: str
    object_uri: str
    checksum_sha256: Sha256Hex
    observed_at: AwareDatetime


class CandidateAction(Contract):
    action_id: str
    action_version: Annotated[int, Field(ge=1)] = 1
    type: str
    source_agent: str
    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    policy_result: PolicyDecision | None = None
    execution_status: str = "CANDIDATE"


class HitlSummary(Contract):
    state: HitlState = "NOT_REQUESTED"
    approval_id: str | None = None


class IncidentContext(Contract):
    incident_id: str
    version: Annotated[int, Field(ge=1)]
    environment: Environment
    status: str
    agent_results: tuple[AgentResult, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    # v5 section 6.3 additions.
    kind: WorkflowKind = "INCIDENT"
    priority: Priority = Priority.P2
    severity: Severity = Severity.MEDIUM
    service: ServiceIdentity | None = None
    latest_signals: dict[str, JsonValue] = Field(default_factory=dict)
    candidate_actions: tuple[CandidateAction, ...] = ()
    hitl: HitlSummary = HitlSummary()
    opened_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None
