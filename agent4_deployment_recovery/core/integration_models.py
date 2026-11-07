"""Versioned Phase 4 contracts for authenticated cross-process integration."""

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from agent4_deployment_recovery.core.execution_models import PatchDirection
from agent4_deployment_recovery.core.schemas import (
    Identifier,
    KubernetesTarget,
    NonEmptyStr,
    RollbackScope,
    Sha256Hex,
    StrictModel,
    TypedOperation,
    VerificationVerdict,
)

Base64Url = Annotated[
    str, StringConstraints(min_length=40, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")
]


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class ApprovalMethod(str, Enum):
    HUMAN_INTERACTIVE = "HUMAN_INTERACTIVE"
    AUTOMATED_POLICY = "AUTOMATED_POLICY"


class ApprovalProvenanceReference(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    provider_id: Identifier
    receipt_id: Identifier
    approver_subject: NonEmptyStr
    method: ApprovalMethod
    evidence_hash: Sha256Hex
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _aware(value, "approved_at")


class ExternalApprovalReceiptV1(StrictModel):
    """Receipt fetched and authenticated by a trusted provenance verifier."""

    schema_version: Literal["1.0"] = "1.0"
    receipt_id: Identifier
    issuer: Identifier
    audience: NonEmptyStr
    approver_subject: NonEmptyStr
    method: ApprovalMethod
    decision: Literal["APPROVED"] = "APPROVED"
    operation_id: Identifier
    correlation_id: Identifier
    target: KubernetesTarget
    authorization_scope_hash: Sha256Hex
    rollback_scope: RollbackScope
    evidence_hash: Sha256Hex
    approved_at: datetime
    expires_at: datetime

    @field_validator("approved_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "approval receipt timestamp")

    @model_validator(mode="after")
    def validate_window(self):
        if self.expires_at <= self.approved_at:
            raise ValueError("approval receipt expiry must follow approval")
        return self


class ExternalAuthorizationClaimsV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    permit_id: Identifier
    issuer: Identifier
    audience: NonEmptyStr
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    operation_id: Identifier
    correlation_id: Identifier
    environment: Identifier
    target: KubernetesTarget
    operation: TypedOperation
    plan_hash: Sha256Hex
    patch_hash: Sha256Hex
    authorization_scope_hash: Sha256Hex
    rollback_scope: RollbackScope
    direction: Literal[PatchDirection.FORWARD] = PatchDirection.FORWARD
    approval: ApprovalProvenanceReference

    @field_validator("issued_at", "not_before", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "authorization timestamp")

    @model_validator(mode="after")
    def validate_window(self):
        if self.not_before < self.issued_at:
            raise ValueError("not_before cannot precede issued_at")
        if self.expires_at <= self.not_before:
            raise ValueError("permit expiry must follow not_before")
        if self.approval.approved_at > self.issued_at:
            raise ValueError("approval provenance cannot postdate permit issuance")
        return self


class SignedAuthorizationEnvelopeV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Identifier
    claims: ExternalAuthorizationClaimsV1
    signature: Base64Url


class AuthorizationBinding(StrictModel):
    audience: NonEmptyStr
    operation_id: Identifier
    correlation_id: Identifier
    environment: Identifier
    target: KubernetesTarget
    operation: TypedOperation
    plan_hash: Sha256Hex
    patch_hash: Sha256Hex
    authorization_scope_hash: Sha256Hex
    rollback_scope: RollbackScope


class ApprovalProvenanceDecision(StrictModel):
    trusted: bool
    verifier_id: Identifier
    checked_at: datetime
    reference: NonEmptyStr | None = None
    reason: NonEmptyStr

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: datetime) -> datetime:
        return _aware(value, "checked_at")

    @model_validator(mode="after")
    def require_reference(self):
        if self.trusted and self.reference is None:
            raise ValueError("trusted approval provenance requires a reference")
        return self


class VerifiedExternalAuthorization(StrictModel):
    permit_id: Identifier
    issuer: Identifier
    verifier_id: Identifier
    claims_hash: Sha256Hex
    approval_reference: NonEmptyStr
    checked_at: datetime
    expires_at: datetime

    @field_validator("checked_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "verified authorization timestamp")

    @model_validator(mode="after")
    def validate_window(self):
        if self.expires_at <= self.checked_at:
            raise ValueError("verified authorization is already expired")
        return self


class RecoveryVerificationRequestV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier
    audience: NonEmptyStr
    execution_id: Identifier
    correlation_id: Identifier
    target_uid: NonEmptyStr
    expected_plan_hash: Sha256Hex
    expected_patch_hash: Sha256Hex
    expected_image: NonEmptyStr | None = None
    expected_version: NonEmptyStr | None = None
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        return _aware(value, "requested_at")

    @model_validator(mode="after")
    def require_expected_workload_version(self):
        if self.expected_image is None and self.expected_version is None:
            raise ValueError("verification requires an expected image or workload version")
        return self


class RecoveryVerificationClaimsV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    verdict_id: Identifier
    issuer: Identifier
    audience: NonEmptyStr
    issued_at: datetime
    expires_at: datetime
    request_id: Identifier
    verification_requested_at: datetime
    observation_started_at: datetime
    observation_ended_at: datetime
    execution_id: Identifier
    correlation_id: Identifier
    target_uid: NonEmptyStr
    expected_plan_hash: Sha256Hex
    expected_patch_hash: Sha256Hex
    expected_image: NonEmptyStr | None = None
    expected_version: NonEmptyStr | None = None
    verdict: Literal[
        VerificationVerdict.VERIFIED,
        VerificationVerdict.FAILED,
        VerificationVerdict.INCONCLUSIVE,
    ]
    evidence_refs: tuple[NonEmptyStr, ...] = ()
    detail: NonEmptyStr

    @field_validator(
        "issued_at",
        "expires_at",
        "verification_requested_at",
        "observation_started_at",
        "observation_ended_at",
    )
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "verification timestamp")

    @model_validator(mode="after")
    def validate_interval(self):
        if self.expected_image is None and self.expected_version is None:
            raise ValueError("verdict requires an expected image or workload version")
        if self.observation_started_at < self.verification_requested_at:
            raise ValueError("observation cannot start before the verification request")
        if self.observation_ended_at < self.observation_started_at:
            raise ValueError("observation interval is reversed")
        if self.issued_at < self.observation_ended_at:
            raise ValueError("verdict cannot be issued before observation ends")
        if self.expires_at <= self.issued_at:
            raise ValueError("verdict expiry must follow issuance")
        return self


class SignedRecoveryVerificationEnvelopeV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Identifier
    claims: RecoveryVerificationClaimsV1
    signature: Base64Url


class ExecutionEventOutcome(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    RECOVERED = "RECOVERED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AuditChainReference(StrictModel):
    sequence: int = Field(ge=0)
    event_hash: Sha256Hex


class ExecutionEventV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: Identifier
    event_type: Literal["EXECUTION_CREATED", "STATE_TRANSITION", "EVIDENCE_RECORDED"]
    execution_id: Identifier
    operation_id: Identifier
    correlation_id: Identifier
    causation_id: Identifier | None = None
    trace_id: Identifier
    idempotency_key: Identifier
    state: NonEmptyStr
    outcome: ExecutionEventOutcome
    evidence_refs: tuple[Sha256Hex, ...] = ()
    occurred_at: datetime
    audit_chain: AuditChainReference

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")


class OutboxRecord(StrictModel):
    outbox_id: int = Field(ge=1)
    event: ExecutionEventV1
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value, "created_at")


class OutboxDeliveryState(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class ClaimedOutboxDelivery(StrictModel):
    outbox_id: int = Field(ge=1)
    delivery_id: Identifier
    event: ExecutionEventV1
    attempt: int = Field(ge=1)
    lease_owner: Identifier
    lease_expires_at: datetime

    @field_validator("lease_expires_at")
    @classmethod
    def validate_lease_expiry(cls, value: datetime) -> datetime:
        return _aware(value, "lease_expires_at")


class OutboxDeliveryStatus(StrictModel):
    outbox_id: int = Field(ge=1)
    delivery_id: Identifier
    state: OutboxDeliveryState
    attempt_count: int = Field(ge=0)
    lease_owner: Identifier | None = None
    lease_expires_at: datetime | None = None
    delivered_at: datetime | None = None
    last_error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_state_shape(self):
        leased = self.lease_owner is not None and self.lease_expires_at is not None
        if self.state == OutboxDeliveryState.CLAIMED and not leased:
            raise ValueError("claimed delivery requires a complete active lease")
        if self.state != OutboxDeliveryState.CLAIMED and (
            self.lease_owner is not None or self.lease_expires_at is not None
        ):
            raise ValueError("only a claimed delivery may contain lease fields")
        if (self.state == OutboxDeliveryState.DELIVERED) != (
            self.delivered_at is not None
        ):
            raise ValueError("delivered_at must exactly match delivered state")
        return self
