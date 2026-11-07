"""Typed, evidence-only integration with the current Agent 6 handoff."""

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import IntegrationContractError
from agent4_deployment_recovery.core.schemas import (
    DeployImageDigest,
    KubernetesTarget,
    NonEmptyStr,
    OperationType,
    RollbackExecution,
    StrictModel,
    VerificationVerdict,
)


class Agent6ModelScope(StrictModel):
    tenant_id: NonEmptyStr
    project_id: NonEmptyStr
    model_id: NonEmptyStr


class Agent6ArtifactLocation(StrictModel):
    kind: Literal["shared-agent6-volume"]
    relative_path: NonEmptyStr


class Agent6DeploymentPreconditions(StrictModel):
    active_model_version: NonEmptyStr
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Agent6ModelDeploymentRequestV1(StrictModel):
    schema_version: Literal["1"]
    kind: Literal["MODEL_DEPLOYMENT_REQUEST"]
    request_id: NonEmptyStr
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_path: NonEmptyStr
    evaluation_ref: NonEmptyStr
    previous_model_version: NonEmptyStr
    review_ref: str | None = None
    scope: Agent6ModelScope
    requires_post_deploy_verification: Literal[True]
    rollback_condition: NonEmptyStr
    artifact_location: Agent6ArtifactLocation
    destination: Literal["deployment-agent"]
    expires_at: AwareDatetime
    preconditions: Agent6DeploymentPreconditions

    @model_validator(mode="after")
    def bind_candidate(self):
        if self.preconditions.candidate_sha256 != self.candidate_sha256:
            raise ValueError("Agent 6 precondition identifies another candidate")
        return self


class Agent6ModelRollbackRequestV1(StrictModel):
    schema_version: Literal["1"]
    kind: Literal["MODEL_ROLLBACK_REQUEST"]
    request_id: NonEmptyStr
    original_job_id: NonEmptyStr
    current_model_version: NonEmptyStr
    restore_model_version: NonEmptyStr
    evidence_ref: NonEmptyStr
    destination: Literal["deployment-agent"]
    scope: Agent6ModelScope


class Agent6ResolvedImage(StrictModel):
    trusted: bool
    resolver_id: NonEmptyStr
    evidence_reference: NonEmptyStr
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: NonEmptyStr
    model_version: NonEmptyStr


@runtime_checkable
class Agent6ArtifactResolver(Protocol):
    def resolve(
        self, request: Agent6ModelDeploymentRequestV1
    ) -> Agent6ResolvedImage:
        """Resolve a qualified model candidate to an attested immutable image."""


@runtime_checkable
class Agent6WorkloadResolver(Protocol):
    def resolve(self, *, scope: Agent6ModelScope) -> tuple[KubernetesTarget, ...]:
        """Resolve one Agent 6 model scope to one immutable serving workload."""


class Agent6DeploymentProposal(StrictModel):
    request_id: NonEmptyStr
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_model_version: NonEmptyStr
    proposed_model_version: NonEmptyStr
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolver_id: NonEmptyStr
    provenance_reference: NonEmptyStr
    target: KubernetesTarget
    operation: DeployImageDigest
    requires_independent_authorization: Literal[True] = True
    constitutes_authorization: Literal[False] = False


def translate_agent6_deployment_request(
    payload_json: str,
    *,
    artifact_resolver: Agent6ArtifactResolver | None,
    workload_resolver: Agent6WorkloadResolver | None,
    at: datetime | None = None,
) -> Agent6DeploymentProposal:
    """Resolve Agent 6 evidence to a digest deployment proposal, never authority."""
    request = Agent6ModelDeploymentRequestV1.model_validate_json(payload_json)
    checked_at = at or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise IntegrationContractError("Agent 6 check time must be timezone-aware")
    if checked_at.astimezone(UTC) >= request.expires_at.astimezone(UTC):
        raise IntegrationContractError("Agent 6 deployment request is expired")
    if artifact_resolver is None:
        raise IntegrationContractError(
            "Agent 6 candidate-to-image provenance resolver is not configured"
        )
    if workload_resolver is None:
        raise IntegrationContractError(
            "Agent 6 serving workload resolver is not configured"
        )
    resolved = artifact_resolver.resolve(request)
    if not resolved.trusted or resolved.candidate_sha256 != request.candidate_sha256:
        raise IntegrationContractError(
            "Agent 6 candidate image provenance is untrusted or mismatched"
        )
    operation = DeployImageDigest(
        operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
        image=resolved.image,
    )
    targets = workload_resolver.resolve(scope=request.scope)
    if len(targets) != 1:
        raise IntegrationContractError(
            "Agent 6 serving workload resolution is absent or ambiguous"
        )
    return Agent6DeploymentProposal(
        request_id=request.request_id,
        candidate_sha256=request.candidate_sha256,
        previous_model_version=request.previous_model_version,
        proposed_model_version=resolved.model_version,
        source_content_hash=canonical_sha256(request),
        resolver_id=resolved.resolver_id,
        provenance_reference=resolved.evidence_reference,
        target=targets[0],
        operation=operation,
    )


class Agent6ResolvedRollback(StrictModel):
    trusted: bool
    resolver_id: NonEmptyStr
    evidence_reference: NonEmptyStr
    current_model_version: NonEmptyStr
    restore_model_version: NonEmptyStr
    source_execution_id: NonEmptyStr
    source_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: KubernetesTarget


@runtime_checkable
class Agent6RollbackResolver(Protocol):
    def resolve(
        self, request: Agent6ModelRollbackRequestV1
    ) -> Agent6ResolvedRollback:
        """Bind Agent 6 model versions to one recoverable Agent 4 execution."""


class Agent6RollbackProposal(StrictModel):
    request_id: NonEmptyStr
    original_job_id: NonEmptyStr
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolver_id: NonEmptyStr
    provenance_reference: NonEmptyStr
    target: KubernetesTarget
    operation: RollbackExecution
    requires_independent_authorization: Literal[True] = True
    constitutes_authorization: Literal[False] = False


def translate_agent6_rollback_request(
    payload_json: str,
    *,
    resolver: Agent6RollbackResolver | None,
) -> Agent6RollbackProposal:
    request = Agent6ModelRollbackRequestV1.model_validate_json(payload_json)
    if resolver is None:
        raise IntegrationContractError(
            "Agent 6 rollback-to-execution resolver is not configured"
        )
    resolved = resolver.resolve(request)
    if (
        not resolved.trusted
        or resolved.current_model_version != request.current_model_version
        or resolved.restore_model_version != request.restore_model_version
    ):
        raise IntegrationContractError(
            "Agent 6 rollback provenance is untrusted or version-mismatched"
        )
    return Agent6RollbackProposal(
        request_id=request.request_id,
        original_job_id=request.original_job_id,
        source_content_hash=canonical_sha256(request),
        resolver_id=resolved.resolver_id,
        provenance_reference=resolved.evidence_reference,
        target=resolved.target,
        operation=RollbackExecution(
            operation_type=OperationType.ROLLBACK_EXECUTION,
            source_execution_id=resolved.source_execution_id,
            source_plan_hash=resolved.source_plan_hash,
        ),
    )


class Agent6DeploymentOutcomeV1(StrictModel):
    """Agent 4 outcome for an integration layer to acknowledge to Agent 6."""

    schema_version: Literal["1.0"] = "1.0"
    request_id: NonEmptyStr
    execution_id: NonEmptyStr
    correlation_id: NonEmptyStr
    action: Literal["DEPLOY", "ROLLBACK"]
    status: Literal["SUCCEEDED", "FAILED", "INCONCLUSIVE"]
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: NonEmptyStr
    evidence_reference: NonEmptyStr
    recovery_verdict: VerificationVerdict
    occurred_at: AwareDatetime
