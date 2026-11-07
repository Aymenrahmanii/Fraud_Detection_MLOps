"""Context Service clients bound to one caller identity.

`AgentContext` exposes only the agent-append endpoints; `OrchestratorContext`
exposes the read and workflow-write endpoints. Both wrap `ContextService`
in-process; `aiops_platform.rpc` provides gRPC clients with the same methods,
so callers do not know (or care) which one they hold.
"""

from datetime import datetime
from typing import Any, Protocol

from pydantic import JsonValue

from aiops_platform.context.service import ContextService, Principal
from aiops_platform.contracts.context import EvidenceReference
from aiops_platform.contracts.enums import AgentId
from aiops_platform.contracts.tasks import AgentResult


class AgentContextPort(Protocol):
    async def append_agent_result(self, result: AgentResult, result_type: str) -> bool: ...

    async def append_evidence(
        self,
        *,
        incident_id: str,
        evidence_type: str,
        summary: str,
        content: JsonValue,
        observed_at: datetime,
    ) -> EvidenceReference: ...


class AgentContext:
    def __init__(self, service: ContextService, agent_id: AgentId) -> None:
        self._service = service
        self._principal = Principal.agent(agent_id)

    async def append_agent_result(self, result: AgentResult, result_type: str) -> bool:
        return await self._service.append_agent_result(self._principal, result, result_type)

    async def append_evidence(self, **kwargs: Any) -> EvidenceReference:
        return await self._service.append_evidence(self._principal, **kwargs)


class OrchestratorContext:
    """Every orchestrator-only endpoint, with the principal bound."""

    def __init__(self, service: ContextService) -> None:
        self._service = service
        self._principal = Principal.orchestrator()

    def __getattr__(self, name: str):
        method = getattr(self._service, name)

        async def call(*args: Any, **kwargs: Any):
            return await method(self._principal, *args, **kwargs)

        return call
