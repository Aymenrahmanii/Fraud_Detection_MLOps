"""Strict, read-only compatibility adapters for current Agent 3/5 payloads.

The adapters deliberately return evidence, never authorization decisions.
"""

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import IntegrationContractError
from agent4_deployment_recovery.core.schemas import (
    MAX_MEMORY_MIB,
    MAX_REPLICAS,
    KubernetesTarget,
    NonEmptyStr,
    OperationType,
    ScaleReplicas,
    SetContainerResources,
    StrictModel,
)


class Agent3TestResults(StrictModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    coverage_pct: float = Field(ge=0, le=100)
    flaky_detected: int = Field(default=0, ge=0)


class Agent3QualityResults(StrictModel):
    gate: Literal["PASSED", "FAILED"]
    new_critical_issues: int = Field(default=0, ge=0)
    duplication_pct: float = Field(default=0, ge=0, le=100)


class Agent3SecurityResults(StrictModel):
    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)
    secrets_found: int = Field(default=0, ge=0)
    highest_cvss: float = Field(default=0, ge=0, le=10)
    policy_exception_required: bool = False


class Agent3ArtifactRef(StrictModel):
    image: str = Field(max_length=512)
    digest: str = Field(default="", max_length=128)


class Agent3ReleaseAssessmentV1(StrictModel):
    schema_version: Literal["1.0"]
    assessment_id: NonEmptyStr
    commit: NonEmptyStr
    artifact: Agent3ArtifactRef
    tests: Agent3TestResults
    quality: Agent3QualityResults
    security: Agent3SecurityResults
    decision: Literal["BLOCK", "ALLOW_WITH_WARNING", "ALLOW"]
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    policy_version: NonEmptyStr
    rationale: str | None = None


class Agent3Evidence(StrictModel):
    assessment_id: NonEmptyStr
    schema_version: Literal["1.0"]
    decision: Literal["BLOCK", "ALLOW_WITH_WARNING", "ALLOW"]
    commit: NonEmptyStr
    image: str
    digest: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    constitutes_authorization: Literal[False] = False
    limitations: tuple[NonEmptyStr, ...]


class Agent5ServiceRef(StrictModel):
    service_id: NonEmptyStr
    namespace: NonEmptyStr
    version: NonEmptyStr


class Agent5ResourceMetrics(StrictModel):
    replicas: int = Field(ge=1)
    ready_replicas: int = Field(ge=0)
    cpu_avg_pct: float = Field(ge=0)
    memory_avg_pct: float = Field(ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    rps: float | None = Field(default=None, ge=0)
    error_rate_pct: float | None = Field(default=None, ge=0, le=100)
    queue_depth: int | None = Field(default=None, ge=0)
    cpu_usage_millicores: float | None = Field(default=None, ge=0)
    cpu_request_millicores: float | None = Field(default=None, ge=0)
    cpu_limit_millicores: float | None = Field(default=None, gt=0)
    memory_usage_mib: float | None = Field(default=None, ge=0)
    memory_request_mib: float | None = Field(default=None, ge=0)
    memory_limit_mib: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ready_cannot_exceed_desired(self):
        if self.ready_replicas > self.replicas:
            raise ValueError("ready_replicas cannot exceed replicas")
        return self


class Agent5Forecast(StrictModel):
    horizon: NonEmptyStr
    rps_expected: float | None = Field(default=None, ge=0)
    confidence_interval: tuple[float, ...] = ()


class Agent5ResourceAction(StrictModel):
    action: Literal["NO_CHANGE", "SCALE_REPLICAS", "INCREASE_MEMORY"]
    from_replicas: int | None = Field(default=None, alias="from", ge=0)
    to_replicas: int | None = Field(default=None, alias="to", ge=0)
    min_replicas: int = Field(alias="min", ge=1)
    max_policy: int = Field(ge=1)
    memory_limit_from_mib: float | None = Field(default=None, ge=0)
    memory_limit_to_mib: float | None = Field(default=None, ge=0)


class Agent5ExpectedEffect(StrictModel):
    latency_reduction_pct: float = Field(ge=0, le=100)
    cpu_target_pct: float | None = Field(default=None, ge=0)
    memory_target_pct: float | None = Field(default=None, ge=0)


class Agent5RecommendationPayload(StrictModel):
    recommendation_id: NonEmptyStr
    window: NonEmptyStr
    current: Agent5ResourceMetrics
    forecast: Agent5Forecast
    recommendation: Agent5ResourceAction
    expected_effect: Agent5ExpectedEffect
    reasons: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    rollback_condition: NonEmptyStr


class Agent5ResourceRecommendationV1(StrictModel):
    message_id: NonEmptyStr
    schema_version: Literal["1.0"]
    correlation_id: NonEmptyStr
    causation_id: str | None = None
    trace_id: NonEmptyStr
    source: Literal["resource-optimization-agent"]
    target: Literal["orchestrator"]
    topic: Literal["resource.recommendation.created"]
    message_type: Literal["RESOURCE_RECOMMENDATION"]
    priority: Literal["P1", "P2", "P3", "P4"]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    timestamp: datetime
    environment: NonEmptyStr
    service: Agent5ServiceRef
    evidence_refs: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    requires_ack: bool
    idempotency_key: NonEmptyStr
    payload: Agent5RecommendationPayload


class Agent5Evidence(StrictModel):
    recommendation_id: NonEmptyStr
    schema_version: Literal["1.0"]
    service_id: NonEmptyStr
    namespace: NonEmptyStr
    action: Literal["NO_CHANGE", "SCALE_REPLICAS", "INCREASE_MEMORY"]
    idempotency_key: NonEmptyStr
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    constitutes_authorization: Literal[False] = False
    limitations: tuple[NonEmptyStr, ...]


def read_agent3_assessment(payload_json: str) -> Agent3Evidence:
    assessment = Agent3ReleaseAssessmentV1.model_validate_json(payload_json)
    return Agent3Evidence(
        assessment_id=assessment.assessment_id,
        schema_version=assessment.schema_version,
        decision=assessment.decision,
        commit=assessment.commit,
        image=assessment.artifact.image,
        digest=assessment.artifact.digest,
        content_hash=canonical_sha256(assessment),
        limitations=(
            "release assessment is evidence, not authorization",
            "current Agent 3 schema does not record strict-mode execution",
            "current Agent 3 schema does not provide trusted issue or expiry timestamps",
        ),
    )


def read_agent5_recommendation(payload_json: str) -> Agent5Evidence:
    recommendation = Agent5ResourceRecommendationV1.model_validate_json(payload_json)
    return Agent5Evidence(
        recommendation_id=recommendation.payload.recommendation_id,
        schema_version=recommendation.schema_version,
        service_id=recommendation.service.service_id,
        namespace=recommendation.service.namespace,
        action=recommendation.payload.recommendation.action,
        idempotency_key=recommendation.idempotency_key,
        content_hash=canonical_sha256(recommendation),
        limitations=(
            "resource recommendation is advisory evidence, not authorization",
            "logical service identity is not an immutable Kubernetes target",
            "rollback_condition is unstructured text",
        ),
    )


class TrustedArtifactProvenance(StrictModel):
    trusted: bool
    verifier_id: NonEmptyStr
    evidence_reference: NonEmptyStr | None = None
    image: NonEmptyStr
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: NonEmptyStr


@runtime_checkable
class Agent3ProvenanceVerifier(Protocol):
    def verify(
        self, assessment: Agent3ReleaseAssessmentV1, *, expected_image: str
    ) -> TrustedArtifactProvenance:
        """Verify digest provenance against a separately trusted CI source."""


class Agent3DeploymentMetadata(StrictModel):
    assessment_id: NonEmptyStr
    correlation_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["ALLOW_WITH_WARNING", "ALLOW"]
    commit: NonEmptyStr
    expected_image: NonEmptyStr
    trusted_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provenance_verifier_id: NonEmptyStr
    provenance_reference: NonEmptyStr
    requires_independent_authorization: Literal[True] = True


def translate_agent3_assessment(
    payload_json: str,
    *,
    expected_image: str,
    provenance_verifier: Agent3ProvenanceVerifier | None = None,
) -> Agent3DeploymentMetadata:
    """Translate Agent 3 evidence without treating its decision as authorization."""
    assessment = Agent3ReleaseAssessmentV1.model_validate_json(payload_json)
    if assessment.decision == "BLOCK":
        raise IntegrationContractError("Agent 3 BLOCK assessment rejects this release")
    if not assessment.artifact.digest:
        raise IntegrationContractError("Agent 3 assessment is missing an image digest")
    if provenance_verifier is None:
        raise IntegrationContractError(
            "trusted CI artifact provenance is not configured"
        )
    provenance = provenance_verifier.verify(assessment, expected_image=expected_image)
    expected_digest = expected_image.partition("@")[2]
    if not expected_digest.startswith("sha256:"):
        raise IntegrationContractError("expected deployment image is not digest-pinned")
    if (
        not provenance.trusted
        or provenance.evidence_reference is None
        or provenance.image != expected_image
        or provenance.digest != expected_digest
        or assessment.artifact.digest != expected_digest
    ):
        raise IntegrationContractError(
            "Agent 3 artifact digest or trusted CI provenance does not match deployment"
        )
    return Agent3DeploymentMetadata(
        assessment_id=assessment.assessment_id,
        correlation_evidence_hash=canonical_sha256(assessment),
        decision=assessment.decision,
        commit=assessment.commit,
        expected_image=expected_image,
        trusted_digest=expected_digest,
        provenance_verifier_id=provenance.verifier_id,
        provenance_reference=provenance.evidence_reference,
    )


@runtime_checkable
class Agent5WorkloadResolver(Protocol):
    def resolve(
        self, *, environment: str, service_id: str, namespace: str
    ) -> tuple[KubernetesTarget, ...]:
        """Resolve a logical Agent 5 service to exact immutable targets."""


class Agent5OperationProposal(StrictModel):
    recommendation_id: NonEmptyStr
    correlation_id: NonEmptyStr
    source_message_id: NonEmptyStr
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: KubernetesTarget | None = None
    operation: ScaleReplicas | SetContainerResources | None = None
    requires_independent_authorization: Literal[True] = True
    constitutes_authorization: Literal[False] = False

    @model_validator(mode="after")
    def target_and_operation_are_paired(self):
        if (self.target is None) != (self.operation is None):
            raise ValueError(
                "target and operation must both be present or both be absent"
            )
        return self


def translate_agent5_recommendation(
    payload_json: str,
    *,
    resolver: Agent5WorkloadResolver | None = None,
) -> Agent5OperationProposal:
    """Produce a bounded proposal; recommendation JSON is never executable input."""
    source = Agent5ResourceRecommendationV1.model_validate_json(payload_json)
    action = source.payload.recommendation
    common = {
        "recommendation_id": source.payload.recommendation_id,
        "correlation_id": source.correlation_id,
        "source_message_id": source.message_id,
        "source_content_hash": canonical_sha256(source),
    }
    if action.action == "NO_CHANGE":
        return Agent5OperationProposal(**common)
    if resolver is None:
        raise IntegrationContractError(
            "Agent 5 mutation proposal requires workload resolution"
        )
    targets = resolver.resolve(
        environment=source.environment,
        service_id=source.service.service_id,
        namespace=source.service.namespace,
    )
    if len(targets) != 1:
        raise IntegrationContractError(
            "Agent 5 workload resolution is absent or ambiguous"
        )
    target = targets[0]
    if target.namespace != source.service.namespace:
        raise IntegrationContractError(
            "resolved target namespace does not match recommendation"
        )
    if action.action == "SCALE_REPLICAS":
        if action.from_replicas is None or action.to_replicas is None:
            raise IntegrationContractError(
                "scale recommendation omitted replica bounds"
            )
        if action.from_replicas != source.payload.current.replicas:
            raise IntegrationContractError(
                "scale recommendation current replicas are inconsistent"
            )
        if not (
            action.min_replicas
            <= action.to_replicas
            <= action.max_policy
            <= MAX_REPLICAS
        ):
            raise IntegrationContractError(
                "scale recommendation is outside bounded policy"
            )
        operation = ScaleReplicas(
            operation_type=OperationType.SCALE_REPLICAS,
            from_replicas=action.from_replicas,
            to_replicas=action.to_replicas,
        )
    else:
        before = action.memory_limit_from_mib
        after = action.memory_limit_to_mib
        if (
            before is None
            or after is None
            or not before.is_integer()
            or not after.is_integer()
        ):
            raise IntegrationContractError(
                "memory recommendation requires exact integer MiB values"
            )
        if (
            source.payload.current.memory_limit_mib != before
            or not before < after <= MAX_MEMORY_MIB
        ):
            raise IntegrationContractError(
                "memory recommendation is inconsistent or out of bounds"
            )
        operation = SetContainerResources(
            operation_type=OperationType.SET_CONTAINER_RESOURCES,
            memory_limit_mib=int(after),
        )
    return Agent5OperationProposal(target=target, operation=operation, **common)
