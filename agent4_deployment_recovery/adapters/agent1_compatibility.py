"""Read-only adapter for the currently implemented Agent 1 recovery event.

The team Agent 1 payload is useful monitoring evidence, but it is not the
signed, execution-bound verdict required by Agent 4's mutation coordinator.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.schemas import NonEmptyStr, StrictModel


class Agent1ServiceIdentity(StrictModel):
    service_id: NonEmptyStr
    namespace: NonEmptyStr
    version: NonEmptyStr


class Agent1RecoveryPayload(StrictModel):
    verification_id: NonEmptyStr
    action_id: NonEmptyStr
    window: NonEmptyStr
    status: Literal["RECOVERED", "NOT_RECOVERED", "INCONCLUSIVE"]
    checks: dict[str, Literal["PASS", "FAIL", "UNKNOWN"]]


class Agent1RecoveryEventV1(StrictModel):
    message_id: NonEmptyStr
    schema_version: Literal["1.0"]
    correlation_id: NonEmptyStr
    causation_id: str | None = None
    trace_id: NonEmptyStr
    source: NonEmptyStr
    target: NonEmptyStr
    topic: Literal["monitoring.recovery.verified"]
    message_type: Literal["RECOVERY_VERIFIED"]
    priority: Literal["P0", "P1", "P2", "P3"]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    timestamp: AwareDatetime
    deadline: AwareDatetime | None = None
    environment: Literal["local", "test", "staging", "production"]
    service: Agent1ServiceIdentity
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    requires_ack: bool
    idempotency_key: NonEmptyStr
    payload: dict[str, JsonValue]


class Agent1RecoveryEvidence(StrictModel):
    message_id: NonEmptyStr
    verification_id: NonEmptyStr
    action_id: NonEmptyStr
    correlation_id: NonEmptyStr
    status: Literal["RECOVERED", "NOT_RECOVERED", "INCONCLUSIVE"]
    observed_at: AwareDatetime
    service_id: NonEmptyStr
    namespace: NonEmptyStr
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    constitutes_trusted_verification: Literal[False] = False
    limitations: tuple[NonEmptyStr, ...]


def read_agent1_recovery_event(payload_json: str) -> Agent1RecoveryEvidence:
    """Validate current Agent 1 evidence without upgrading it to trust."""
    event = Agent1RecoveryEventV1.model_validate_json(payload_json)
    payload = Agent1RecoveryPayload.model_validate(event.payload)
    return Agent1RecoveryEvidence(
        message_id=event.message_id,
        verification_id=payload.verification_id,
        action_id=payload.action_id,
        correlation_id=event.correlation_id,
        status=payload.status,
        observed_at=event.timestamp,
        service_id=event.service.service_id,
        namespace=event.service.namespace,
        content_hash=canonical_sha256(event),
        limitations=(
            "current Agent 1 event is not signed for Agent 4",
            "current Agent 1 event is not bound to execution ID, target UID, plan hash, or patch hash",
            "current Agent 1 event does not carry exact observation start/end timestamps",
        ),
    )
