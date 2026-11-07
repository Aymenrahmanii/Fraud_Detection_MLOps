"""Context Service access rules (v5 sections 4.1 and 6).

* The Orchestrator may read the full incident context and record decisions.
* An agent may only append its own results and evidence. It has no read
  endpoint at all, and an append must name the calling agent.
* The HITL Service may only write approvals.

Callers are identified by a `Principal`. Behind gRPC the principal comes from
the authenticated peer (mTLS identity or a signed service token, see
`identity.py`); it is never taken from the request body.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import JsonValue

from aiops_platform.context.store import ContextStore
from aiops_platform.contracts.context import (
    CandidateAction,
    EvidenceReference,
    IncidentContext,
    WorkflowKind,
)
from aiops_platform.contracts.enums import AgentId, Environment, Priority, Severity
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    HITL = "hitl-service"
    DASHBOARD = "dashboard"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role

    @classmethod
    def orchestrator(cls) -> "Principal":
        return cls("orchestrator", Role.ORCHESTRATOR)

    @classmethod
    def agent(cls, agent_id: AgentId) -> "Principal":
        return cls(agent_id.value, Role.AGENT)

    @classmethod
    def hitl(cls) -> "Principal":
        return cls("hitl-service", Role.HITL)

    @classmethod
    def dashboard(cls) -> "Principal":
        return cls("dashboard", Role.DASHBOARD)


class AccessDenied(PermissionError):
    pass


def _require(principal: Principal, *roles: Role) -> None:
    if principal.role not in roles:
        raise AccessDenied(f"{principal.name} ({principal.role}) may not call this endpoint")


class ContextService:
    def __init__(self, store: ContextStore) -> None:
        self._store = store

    # ---- orchestrator: read + authoritative workflow writes -------------

    async def open_incident(
        self,
        principal: Principal,
        *,
        incident_id: str,
        kind: WorkflowKind,
        service: ServiceIdentity,
        environment: Environment,
        severity: Severity,
        priority: Priority,
        signals: dict[str, JsonValue],
    ) -> tuple[IncidentContext, bool]:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.open_incident(
            incident_id=incident_id,
            kind=kind,
            service=service,
            environment=environment,
            severity=severity,
            priority=priority,
            signals=signals,
            actor=principal.name,
        )

    async def get_incident_context(self, principal: Principal, incident_id: str) -> IncidentContext:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.get_incident_context(incident_id)

    async def get_latest_agent_results(
        self, principal: Principal, incident_id: str, agent_ids: Sequence[str] = ()
    ) -> tuple[AgentResult, ...]:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.get_latest_agent_results(incident_id, agent_ids)

    async def update_incident(self, principal: Principal, incident_id: str, **changes: Any) -> IncidentContext:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.update_incident(incident_id, actor=principal.name, **changes)

    async def record_action(self, principal: Principal, incident_id: str, action: CandidateAction) -> None:
        _require(principal, Role.ORCHESTRATOR)
        await self._store.record_action(incident_id, action, actor=principal.name)

    async def update_action(self, principal: Principal, incident_id: str, action_id: str, action_version: int, **changes: Any) -> None:
        _require(principal, Role.ORCHESTRATOR)
        await self._store.update_action(
            incident_id, action_id, action_version, actor=principal.name, **changes
        )

    async def record_decision(
        self, principal: Principal, incident_id: str, *, stage: str, decision_type: str, payload: dict[str, JsonValue]
    ) -> str:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.record_decision(
            incident_id, stage=stage, decision_type=decision_type, payload=payload, actor=principal.name
        )

    async def record_hitl_decision(
        self, principal: Principal, incident_id: str, *, approval_id: str, decision: str, approver_id: str | None, comment: str
    ) -> str:
        """The Orchestrator's own record of the human decision it acted on."""
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.record_decision(
            incident_id,
            stage="HITL",
            decision_type=f"HITL_{decision}",
            payload={"approval_id": approval_id, "approver_id": approver_id, "comment": comment},
            actor=principal.name,
        )

    async def record_model_event(self, principal: Principal, incident_id: str, **event: Any) -> str:
        _require(principal, Role.ORCHESTRATOR)
        return await self._store.record_model_event(incident_id, actor=principal.name, **event)

    async def read_evidence(self, principal: Principal, evidence_id: str) -> tuple[EvidenceReference, JsonValue]:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD)
        return await self._store.read_evidence(evidence_id)

    # ---- agents: append only --------------------------------------------

    async def append_agent_result(self, principal: Principal, result: AgentResult, result_type: str) -> bool:
        _require(principal, Role.AGENT)
        if result.agent_id.value != principal.name:
            raise AccessDenied(f"{principal.name} may not append a result for {result.agent_id}")
        return await self._store.append_agent_result(result, result_type, actor=principal.name)

    async def append_evidence(
        self,
        principal: Principal,
        *,
        incident_id: str,
        evidence_type: str,
        summary: str,
        content: JsonValue,
        observed_at: datetime,
    ) -> EvidenceReference:
        _require(principal, Role.AGENT, Role.ORCHESTRATOR)
        return await self._store.append_evidence(
            incident_id=incident_id,
            source=principal.name,
            evidence_type=evidence_type,
            summary=summary,
            content=content,
            observed_at=observed_at,
            actor=principal.name,
        )

    # ---- HITL Service ----------------------------------------------------

    async def upsert_approval(self, principal: Principal, row: dict[str, Any]) -> None:
        _require(principal, Role.HITL)
        await self._store.upsert_approval(row, actor=principal.name)

    async def get_approval(self, principal: Principal, approval_id: str) -> dict[str, Any] | None:
        _require(principal, Role.HITL, Role.ORCHESTRATOR)
        return await self._store.get_approval(approval_id)

    # ---- dashboard: read-only views -------------------------------------

    async def list_incidents(self, principal: Principal, limit: int = 100) -> list[IncidentContext]:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD)
        return await self._store.list_incidents(limit)

    async def dashboard_incident(self, principal: Principal, incident_id: str) -> IncidentContext:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD)
        return await self._store.get_incident_context(incident_id)

    async def list_approvals(self, principal: Principal, state: str | None = None) -> list[dict[str, Any]]:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD, Role.HITL)
        return await self._store.list_approvals(state=state)

    async def audit_trail(self, principal: Principal, incident_id: str) -> list[dict[str, Any]]:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD)
        return await self._store.audit_trail(incident_id)

    async def verify_audit_chain(self, principal: Principal, incident_id: str) -> bool:
        _require(principal, Role.ORCHESTRATOR, Role.DASHBOARD)
        return await self._store.verify_audit_chain(incident_id)
