from typing import Protocol

from agent1_monitoring.shared_contracts.payloads import AnomalyDetected, RecoveryVerified
from agent1_monitoring.shared_contracts.tasks import TaskContext


class AnomalyDetector(Protocol):
    async def detect(self, context: TaskContext) -> AnomalyDetected | None: ...


class RecoveryVerifier(Protocol):
    async def verify(self, action_id: str, context: TaskContext) -> RecoveryVerified: ...
