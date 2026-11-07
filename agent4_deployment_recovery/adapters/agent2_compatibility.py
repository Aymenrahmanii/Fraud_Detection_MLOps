"""Typed, non-authorizing translation of current Agent 2 recommendations."""

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import IntegrationContractError
from agent4_deployment_recovery.core.schemas import (
    DeployImageDigest,
    KubernetesTarget,
    NonEmptyStr,
    OperationType,
    RollbackExecution,
    ScaleReplicas,
    StrictModel,
)


class Agent2ActionTarget(StrictModel):
    service: NonEmptyStr
    namespace: NonEmptyStr


class Agent2ActionIntent(StrictModel):
    action_id: NonEmptyStr
    action_version: int = Field(default=1, ge=1)
    incident_id: NonEmptyStr
    action: Literal[
        "DEPLOY",
        "ROLLING_RESTART",
        "ROLLBACK",
        "CANARY",
        "BLUE_GREEN_SWITCH",
        "TRAFFIC_SHIFT",
        "SCALE_REPLICAS",
        "PROMOTE_MODEL",
    ]
    environment: Literal["local", "test", "staging", "production"]
    target: Agent2ActionTarget
    parameters: dict[str, JsonValue]
    preconditions: tuple[str, ...] = ()
    verification_profile: NonEmptyStr


class Agent2Hypothesis(StrictModel):
    rank: int = Field(ge=1)
    code: NonEmptyStr
    probability: float = Field(ge=0, le=1)
    evidence_refs: tuple[str, ...]


class Agent2DiagnosisCompleted(StrictModel):
    diagnosis_id: NonEmptyStr
    hypotheses: tuple[Agent2Hypothesis, ...]
    primary_root_cause: NonEmptyStr
    recommended_actions: tuple[Agent2ActionIntent, ...]
    rationale_summary: NonEmptyStr
    limitations: tuple[str, ...] = ()
    requires_additional_evidence: bool = False


class Agent2DeployParameters(StrictModel):
    image: NonEmptyStr


class Agent2ScaleParameters(StrictModel):
    from_replicas: int = Field(ge=0)
    to_replicas: int = Field(ge=0)


class Agent2RollbackParameters(StrictModel):
    source_execution_id: NonEmptyStr
    source_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@runtime_checkable
class Agent2WorkloadResolver(Protocol):
    def resolve(
        self, *, environment: str, service_id: str, namespace: str
    ) -> tuple[KubernetesTarget, ...]:
        """Resolve an Agent 2 logical service to one immutable target."""


class Agent2OperationProposal(StrictModel):
    diagnosis_id: NonEmptyStr
    incident_id: NonEmptyStr
    action_id: NonEmptyStr
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: KubernetesTarget
    operation: DeployImageDigest | ScaleReplicas | RollbackExecution
    requires_independent_authorization: Literal[True] = True
    constitutes_authorization: Literal[False] = False


def translate_agent2_diagnosis(
    payload_json: str,
    *,
    action_id: str,
    resolver: Agent2WorkloadResolver | None,
) -> Agent2OperationProposal:
    """Translate one exact supported action; never execute Agent 2 parameters."""
    diagnosis = Agent2DiagnosisCompleted.model_validate_json(payload_json)
    matches = tuple(
        item for item in diagnosis.recommended_actions if item.action_id == action_id
    )
    if len(matches) != 1:
        raise IntegrationContractError(
            "Agent 2 action selection is absent or ambiguous"
        )
    intent = matches[0]
    if diagnosis.requires_additional_evidence:
        raise IntegrationContractError(
            "Agent 2 requires additional evidence before remediation"
        )
    if resolver is None:
        raise IntegrationContractError(
            "Agent 2 remediation requires exact workload resolution"
        )
    targets = resolver.resolve(
        environment=intent.environment,
        service_id=intent.target.service,
        namespace=intent.target.namespace,
    )
    if len(targets) != 1:
        raise IntegrationContractError(
            "Agent 2 workload resolution is absent or ambiguous"
        )
    target = targets[0]
    if target.namespace != intent.target.namespace:
        raise IntegrationContractError(
            "Agent 2 target namespace differs from the resolved workload"
        )

    if intent.action == "DEPLOY":
        parameters = Agent2DeployParameters.model_validate(intent.parameters)
        operation = DeployImageDigest(
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            image=parameters.image,
        )
    elif intent.action == "SCALE_REPLICAS":
        parameters = Agent2ScaleParameters.model_validate(intent.parameters)
        operation = ScaleReplicas(
            operation_type=OperationType.SCALE_REPLICAS,
            from_replicas=parameters.from_replicas,
            to_replicas=parameters.to_replicas,
        )
    elif intent.action == "ROLLBACK":
        parameters = Agent2RollbackParameters.model_validate(intent.parameters)
        operation = RollbackExecution(
            operation_type=OperationType.ROLLBACK_EXECUTION,
            source_execution_id=parameters.source_execution_id,
            source_plan_hash=parameters.source_plan_hash,
        )
    else:
        raise IntegrationContractError(
            f"Agent 2 action {intent.action} is unsupported by Agent 4"
        )
    return Agent2OperationProposal(
        diagnosis_id=diagnosis.diagnosis_id,
        incident_id=intent.incident_id,
        action_id=intent.action_id,
        source_content_hash=canonical_sha256(diagnosis),
        target=target,
        operation=operation,
    )
