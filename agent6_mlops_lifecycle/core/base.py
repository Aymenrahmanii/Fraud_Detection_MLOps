"""Minimal task boundary used by Agent 6; other agents are not bundled."""

from abc import ABC, abstractmethod
from typing import Protocol

from agent6_mlops_lifecycle.contracts.enums import AgentId
from agent6_mlops_lifecycle.contracts.tasks import AgentResult, AgentTask


class Agent(Protocol):
    agent_id: AgentId

    async def handle(self, task: AgentTask) -> AgentResult: ...


class BaseAgent(ABC):
    agent_id: AgentId
    responsibility: str

    @abstractmethod
    async def handle(self, task: AgentTask) -> AgentResult:
        raise NotImplementedError
