"""Strict Phase 0 contracts for Agent 4.

These models describe authorized intent and execution bookkeeping. They do not
grant authorization and they contain no infrastructure mutation implementation.
"""

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from agent4_deployment_recovery.core.canonical import canonical_sha256

SCHEMA_VERSION = "1.0"
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_REPLICAS = 100
MAX_CPU_MILLICORES = 1_000_000
MAX_MEMORY_MIB = 1_048_576

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
KubernetesName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$",
    ),
]
ContainerName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
    ),
]
OpaqueKubernetesIdentity = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=256, pattern=r"^\S+$"
    ),
]


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OperationType(str, Enum):
    DEPLOY_IMAGE_DIGEST = "DEPLOY_IMAGE_DIGEST"
    SCALE_REPLICAS = "SCALE_REPLICAS"
    SET_CONTAINER_RESOURCES = "SET_CONTAINER_RESOURCES"
    ROLLBACK_EXECUTION = "ROLLBACK_EXECUTION"
    NO_OP = "NO_OP"


class ExecutionState(str, Enum):
    RECEIVED = "RECEIVED"
    AUTHORIZED = "AUTHORIZED"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    PLANNED = "PLANNED"
    DRY_RUN_PASSED = "DRY_RUN_PASSED"
    EXECUTING = "EXECUTING"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    OBSERVING = "OBSERVING"
    AWAITING_AGENT1 = "AWAITING_AGENT1"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLBACK_AWAITING_AGENT1 = "ROLLBACK_AWAITING_AGENT1"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    VERIFICATION_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"


class VerificationVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNAVAILABLE = "UNAVAILABLE"


class PlanStrategy(str, Enum):
    NATIVE_ROLLING = "NATIVE_ROLLING"
    DIRECT_RESOURCE_PATCH = "DIRECT_RESOURCE_PATCH"
    EXACT_STATE_ROLLBACK = "EXACT_STATE_ROLLBACK"
    NO_OP = "NO_OP"


class PlanAction(str, Enum):
    VALIDATE_AUTHORIZATION = "VALIDATE_AUTHORIZATION"
    VALIDATE_TARGET = "VALIDATE_TARGET"
    CAPTURE_CURRENT_STATE = "CAPTURE_CURRENT_STATE"
    SERVER_DRY_RUN = "SERVER_DRY_RUN"
    APPLY_TYPED_OPERATION = "APPLY_TYPED_OPERATION"
    OBSERVE_ROLLOUT = "OBSERVE_ROLLOUT"
    REQUEST_AGENT1_VERIFICATION = "REQUEST_AGENT1_VERIFICATION"
    RECORD_NO_OP = "RECORD_NO_OP"


class RollbackField(str, Enum):
    IMAGE = "image"
    REPLICAS = "replicas"
    RESOURCES = "resources"


class KubernetesTarget(StrictModel):
    cluster_id: Identifier
    namespace: KubernetesName
    kind: Literal["Deployment"] = "Deployment"
    name: KubernetesName
    container: ContainerName
    uid: OpaqueKubernetesIdentity
    expected_resource_version: OpaqueKubernetesIdentity

    @property
    def workload_key(self) -> str:
        # UID remains an immutable-state precondition, while exclusion is keyed
        # to the logical workload so delete/recreate races cannot bypass it.
        return (
            f"{self.cluster_id}:{self.namespace}:{self.kind}:"
            f"{self.name}:{self.container}"
        )


class RollbackScope(StrictModel):
    authorized: bool = False
    target_uid: Identifier | None = None
    restore_fields: tuple[RollbackField, ...] = ()
    max_attempts: int = Field(default=1, ge=1, le=3)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.authorized:
            if (
                self.target_uid is None
                or self.expires_at is None
                or not self.restore_fields
            ):
                raise ValueError(
                    "authorized rollback requires target_uid, expires_at, and restore_fields"
                )
            _require_aware(self.expires_at, "rollback_scope.expires_at")
            if self.expires_at <= datetime.now(UTC):
                raise ValueError("rollback scope is expired")
            if len(set(self.restore_fields)) != len(self.restore_fields):
                raise ValueError("rollback restore_fields must be unique")
        elif (
            self.target_uid is not None
            or self.expires_at is not None
            or self.restore_fields
        ):
            raise ValueError("unauthorized rollback scope cannot contain permissions")
        return self


class ApprovalReceipt(StrictModel):
    approval_id: Identifier
    issuer: Identifier
    approver_subject: NonEmptyStr
    decision: Literal["APPROVED"] = "APPROVED"
    operation_id: Identifier
    scope_hash: Sha256Hex
    approved_at: datetime
    expires_at: datetime
    rollback_scope: RollbackScope = Field(default_factory=RollbackScope)

    @model_validator(mode="after")
    def validate_times(self):
        approved_at = _require_aware(self.approved_at, "approved_at")
        expires_at = _require_aware(self.expires_at, "expires_at")
        now = datetime.now(UTC)
        if approved_at > now + MAX_CLOCK_SKEW:
            raise ValueError("approval timestamp is in the future")
        if expires_at <= approved_at:
            raise ValueError("approval expiry must be after approval time")
        if expires_at <= now:
            raise ValueError("approval is expired")
        if (
            self.rollback_scope.authorized
            and self.rollback_scope.expires_at > expires_at
        ):
            raise ValueError("rollback authorization cannot outlive the approval")
        return self


class EvidenceReference(StrictModel):
    source_type: Literal["AGENT3_RELEASE_ASSESSMENT", "AGENT5_RESOURCE_RECOMMENDATION"]
    evidence_id: Identifier
    schema_version: Literal["1.0"]
    content_hash: Sha256Hex


class DeployImageDigest(StrictModel):
    operation_type: Literal[OperationType.DEPLOY_IMAGE_DIGEST]
    image: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=73,
            max_length=512,
            pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$",
        ),
    ]

    @field_validator("image")
    @classmethod
    def reject_tag_even_when_digest_is_present(cls, value: str) -> str:
        image_name = value.split("@", 1)[0]
        final_component = image_name.rsplit("/", 1)[-1]
        if ":" in final_component:
            raise ValueError(
                "image must not contain a mutable tag; use repository@sha256:digest"
            )
        return value


class ScaleReplicas(StrictModel):
    operation_type: Literal[OperationType.SCALE_REPLICAS]
    from_replicas: int = Field(ge=0, le=MAX_REPLICAS)
    to_replicas: int = Field(ge=0, le=MAX_REPLICAS)

    @model_validator(mode="after")
    def require_change(self):
        if self.from_replicas == self.to_replicas:
            raise ValueError("replica operation must change the replica count")
        return self


class SetContainerResources(StrictModel):
    operation_type: Literal[OperationType.SET_CONTAINER_RESOURCES]
    cpu_request_millicores: int | None = Field(
        default=None, ge=1, le=MAX_CPU_MILLICORES
    )
    cpu_limit_millicores: int | None = Field(default=None, ge=1, le=MAX_CPU_MILLICORES)
    memory_request_mib: int | None = Field(default=None, ge=1, le=MAX_MEMORY_MIB)
    memory_limit_mib: int | None = Field(default=None, ge=1, le=MAX_MEMORY_MIB)

    @model_validator(mode="after")
    def validate_resources(self):
        values = (
            self.cpu_request_millicores,
            self.cpu_limit_millicores,
            self.memory_request_mib,
            self.memory_limit_mib,
        )
        if all(value is None for value in values):
            raise ValueError("at least one resource value is required")
        if (
            self.cpu_request_millicores is not None
            and self.cpu_limit_millicores is not None
            and self.cpu_request_millicores > self.cpu_limit_millicores
        ):
            raise ValueError("CPU request cannot exceed CPU limit")
        if (
            self.memory_request_mib is not None
            and self.memory_limit_mib is not None
            and self.memory_request_mib > self.memory_limit_mib
        ):
            raise ValueError("memory request cannot exceed memory limit")
        return self


class RollbackExecution(StrictModel):
    operation_type: Literal[OperationType.ROLLBACK_EXECUTION]
    source_execution_id: Identifier
    source_plan_hash: Sha256Hex


class NoOp(StrictModel):
    operation_type: Literal[OperationType.NO_OP]
    reason: NonEmptyStr


TypedOperation = Annotated[
    DeployImageDigest
    | ScaleReplicas
    | SetContainerResources
    | RollbackExecution
    | NoOp,
    Field(discriminator="operation_type"),
]


def authorization_scope_hash(
    *,
    operation_id: str,
    target: KubernetesTarget,
    operation: TypedOperation,
    rollback_scope: RollbackScope,
) -> str:
    """Hash the exact action and rollback permissions an approval covers."""
    return canonical_sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "target": target,
            "operation": operation,
            "rollback_scope": rollback_scope,
        }
    )


class AuthorizedOperation(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    operation_id: Identifier
    idempotency_key: Identifier
    correlation_id: Identifier
    trace_id: Identifier
    issuer: Identifier
    issued_at: datetime
    expires_at: datetime
    environment: Identifier
    target: KubernetesTarget
    operation: TypedOperation
    approval: ApprovalReceipt
    evidence: tuple[EvidenceReference, ...] = ()
    authorization_token: SecretStr = Field(repr=False, exclude=True, min_length=16)

    @model_validator(mode="after")
    def validate_authorized_intent(self):
        issued_at = _require_aware(self.issued_at, "issued_at")
        expires_at = _require_aware(self.expires_at, "expires_at")
        now = datetime.now(UTC)
        if issued_at > now + MAX_CLOCK_SKEW:
            raise ValueError("operation issue timestamp is in the future")
        if expires_at <= issued_at:
            raise ValueError("operation expiry must be after issue time")
        if expires_at <= now:
            raise ValueError("operation is expired")
        if self.approval.operation_id != self.operation_id:
            raise ValueError("approval operation_id does not match the operation")
        if self.approval.expires_at < expires_at:
            raise ValueError("approval expires before the operation")
        rollback_scope = self.approval.rollback_scope
        if rollback_scope.authorized and rollback_scope.target_uid != self.target.uid:
            raise ValueError(
                "rollback scope target does not match the immutable target UID"
            )
        required_rollback_field = {
            OperationType.DEPLOY_IMAGE_DIGEST: RollbackField.IMAGE,
            OperationType.SCALE_REPLICAS: RollbackField.REPLICAS,
            OperationType.SET_CONTAINER_RESOURCES: RollbackField.RESOURCES,
        }.get(OperationType(self.operation.operation_type))
        if required_rollback_field is not None:
            if not rollback_scope.authorized:
                raise ValueError(
                    "a mutating forward operation requires pre-authorized rollback"
                )
            if rollback_scope.restore_fields != (required_rollback_field,):
                raise ValueError(
                    "rollback scope must grant only the exact affected field"
                )
        elif rollback_scope.authorized:
            raise ValueError(
                "NO_OP and ROLLBACK_EXECUTION cannot carry nested rollback authority"
            )
        expected_scope = authorization_scope_hash(
            operation_id=self.operation_id,
            target=self.target,
            operation=self.operation,
            rollback_scope=rollback_scope,
        )
        if self.approval.scope_hash != expected_scope:
            raise ValueError("approval scope_hash does not cover this exact operation")
        return self

    def ensure_fresh(self, *, at: datetime | None = None) -> None:
        current = _require_aware(at or datetime.now(UTC), "current time")
        if self.expires_at <= current:
            raise ValueError("operation is expired")
        if self.approval.expires_at <= current:
            raise ValueError("approval is expired")
        rollback_scope = self.approval.rollback_scope
        if (
            rollback_scope.authorized
            and rollback_scope.expires_at is not None
            and rollback_scope.expires_at <= current
        ):
            raise ValueError("rollback authorization is expired")


class PlanStep(StrictModel):
    sequence: int = Field(ge=1)
    action: PlanAction
    description: NonEmptyStr
    mutates_infrastructure: bool = False


class DeploymentPlan(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plan_id: Identifier
    operation_id: Identifier
    idempotency_key: Identifier
    correlation_id: Identifier
    trace_id: Identifier
    environment: Identifier
    target: KubernetesTarget
    operation: TypedOperation
    strategy: PlanStrategy
    created_at: datetime
    expires_at: datetime
    authorization_scope_hash: Sha256Hex
    rollback_scope: RollbackScope
    preconditions: tuple[NonEmptyStr, ...]
    steps: tuple[PlanStep, ...]
    requires_agent1_verification: bool
    dry_run_only: Literal[True] = True
    plan_hash: Sha256Hex

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_datetime(cls, value: datetime) -> datetime:
        return _require_aware(value, "plan timestamp")

    @model_validator(mode="after")
    def validate_plan(self):
        if self.expires_at <= self.created_at:
            raise ValueError("plan expiry must be after creation")
        if self.expires_at <= datetime.now(UTC):
            raise ValueError("plan is expired")
        if not self.steps:
            raise ValueError("plan must contain at least one step")
        if tuple(step.sequence for step in self.steps) != tuple(
            range(1, len(self.steps) + 1)
        ):
            raise ValueError("plan step sequence must be contiguous and start at 1")
        expected = canonical_sha256(
            self.model_dump(mode="python", exclude={"plan_hash"})
        )
        if self.plan_hash != expected:
            raise ValueError("plan_hash does not match canonical plan content")
        return self


class RecoveryVerification(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    verification_id: Identifier
    execution_id: Identifier
    correlation_id: Identifier
    verifier_id: Identifier
    verdict: VerificationVerdict
    observed_at: datetime
    target_uid: Identifier
    observed_version: NonEmptyStr | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = ()
    detail: NonEmptyStr
    signature: NonEmptyStr | None = None

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        observed_at = _require_aware(value, "observed_at")
        if observed_at > datetime.now(UTC) + MAX_CLOCK_SKEW:
            raise ValueError("observed_at is in the future")
        return observed_at


class ExecutionRecord(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    execution_id: Identifier
    operation_id: Identifier
    plan_id: Identifier
    plan_hash: Sha256Hex
    idempotency_key: Identifier
    correlation_id: Identifier
    trace_id: Identifier
    target: KubernetesTarget
    operation_type: OperationType
    state: ExecutionState = ExecutionState.RECEIVED
    approval_id: Identifier
    authorization_verifier_id: Identifier | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(default=1, ge=1)
    failure_reason: NonEmptyStr | None = None
    verification: RecoveryVerification | None = None
    rollback_execution_id: Identifier | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_record_datetime(cls, value: datetime) -> datetime:
        return _require_aware(value, "execution timestamp")

    @model_validator(mode="after")
    def validate_record(self):
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.verification is not None:
            if self.verification.execution_id != self.execution_id:
                raise ValueError("verification execution_id does not match record")
            if self.verification.correlation_id != self.correlation_id:
                raise ValueError("verification correlation_id does not match record")
            if self.verification.target_uid != self.target.uid:
                raise ValueError("verification target_uid does not match record")
        if (
            self.state == ExecutionState.SUCCEEDED
            and self.operation_type != OperationType.NO_OP
            and (
                self.verification is None
                or self.verification.verdict != VerificationVerdict.VERIFIED
            )
        ):
            raise ValueError("successful mutation requires VERIFIED recovery evidence")
        if self.state == ExecutionState.VERIFICATION_INCONCLUSIVE and (
            self.verification is None
            or self.verification.verdict
            not in {VerificationVerdict.INCONCLUSIVE, VerificationVerdict.UNAVAILABLE}
        ):
            raise ValueError(
                "VERIFICATION_INCONCLUSIVE requires INCONCLUSIVE or UNAVAILABLE evidence"
            )
        if self.state == ExecutionState.ROLLED_BACK and (
            self.verification is None
            or self.verification.verdict != VerificationVerdict.VERIFIED
        ):
            raise ValueError("completed rollback requires VERIFIED Agent 1 evidence")
        if (
            self.state
            in {
                ExecutionState.REJECTED,
                ExecutionState.FAILED,
                ExecutionState.ROLLBACK_FAILED,
            }
            and self.failure_reason is None
        ):
            raise ValueError(f"{self.state.value} requires a failure reason")
        return self
