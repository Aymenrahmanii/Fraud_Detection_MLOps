"""Strict Phase 3 contracts for controlled execution and observation."""

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent4_deployment_recovery.core.canonical import canonical_json, canonical_sha256
from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentSnapshot,
    JsonPatchOperation,
    RollbackSnapshot,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.schemas import (
    Identifier,
    NonEmptyStr,
    OpaqueKubernetesIdentity,
    OperationType,
    Sha256Hex,
    StrictModel,
)


class PatchDirection(str, Enum):
    FORWARD = "FORWARD"
    ROLLBACK = "ROLLBACK"


class MutationOutcome(str, Enum):
    APPLIED = "APPLIED"
    UNCERTAIN = "UNCERTAIN"


class ReconciliationOutcome(str, Enum):
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    DIVERGED = "DIVERGED"


class RolloutOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    TIMEOUT = "TIMEOUT"
    STALLED = "STALLED"
    FAILED = "FAILED"
    STALE = "STALE"
    API_ERROR = "API_ERROR"
    CANCELLED = "CANCELLED"


def canonical_patch_bytes(patch: tuple[JsonPatchOperation, ...]) -> bytes:
    """Return the exact deterministic RFC 6902 body used for dry-run and apply."""
    payload = [item.model_dump(mode="python", exclude_none=True) for item in patch]
    return canonical_json(payload).encode("utf-8")


class ValidatedPatchArtifact(StrictModel):
    """Persistable, immutable patch body bound to a plan and dry-run result."""

    schema_version: Literal["1.0"] = "1.0"
    operation_id: Identifier
    plan_hash: Sha256Hex
    authorization_scope_hash: Sha256Hex
    rollback_scope_hash: Sha256Hex | None = None
    preflight_hash: Sha256Hex
    direction: PatchDirection
    target_uid: OpaqueKubernetesIdentity
    base_resource_version: OpaqueKubernetesIdentity
    base_generation: int = Field(ge=1)
    patch: tuple[JsonPatchOperation, ...]
    patch_bytes_utf8: str = Field(min_length=1, max_length=16384)
    patch_bytes_hash: Sha256Hex
    source_preview_hash: Sha256Hex | None = None
    dry_run_result_hash: Sha256Hex | None = None
    rollback_snapshot: RollbackSnapshot

    @model_validator(mode="after")
    def validate_exact_bytes_and_shape(self):
        exact = canonical_patch_bytes(self.patch)
        try:
            supplied = self.patch_bytes_utf8.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("patch bytes are not valid UTF-8") from exc
        if supplied != exact:
            raise ValueError("persisted patch bytes are not the canonical typed patch")
        if hashlib.sha256(supplied).hexdigest() != self.patch_bytes_hash:
            raise ValueError("patch byte hash does not match exact patch bytes")
        if len(self.patch) < 2:
            raise ValueError("patch is missing immutable identity preconditions")
        first, second = self.patch[:2]
        if (first.op, first.path, first.value) != (
            "test",
            "/metadata/uid",
            self.target_uid,
        ):
            raise ValueError("patch must begin with the exact UID test")
        if (second.op, second.path, second.value) != (
            "test",
            "/metadata/resourceVersion",
            self.base_resource_version,
        ):
            raise ValueError("patch must include the exact resourceVersion test")
        if self.rollback_snapshot.target_uid != self.target_uid:
            raise ValueError("rollback snapshot belongs to a different target")
        if self.direction == PatchDirection.FORWARD:
            if self.source_preview_hash is None or self.dry_run_result_hash is None:
                raise ValueError(
                    "forward patch requires preview and dry-run evidence hashes"
                )
            TypedPatchPreview(
                operation_id=self.operation_id,
                target_uid=self.target_uid,
                base_resource_version=self.base_resource_version,
                patch=self.patch,
                rollback=self.rollback_snapshot,
                patch_hash=self.source_preview_hash,
            )
            if self.rollback_scope_hash is not None:
                raise ValueError(
                    "forward patch cannot carry a rollback-scope permit hash"
                )
        else:
            if self.rollback_scope_hash is None:
                raise ValueError("rollback patch requires an exact rollback-scope hash")
            index = self.rollback_snapshot.container_index
            root = f"/spec/template/spec/containers/{index}"
            shape = tuple((item.op, item.path) for item in self.patch)
            common = (("test", "/metadata/uid"), ("test", "/metadata/resourceVersion"))
            operation = self.rollback_snapshot.operation_type
            if operation == OperationType.SCALE_REPLICAS:
                expected = common + (
                    ("test", "/spec/replicas"),
                    ("replace", "/spec/replicas"),
                )
            elif operation == OperationType.DEPLOY_IMAGE_DIGEST:
                expected = common + (
                    ("test", f"{root}/name"),
                    ("test", f"{root}/image"),
                    ("replace", f"{root}/image"),
                )
            elif operation == OperationType.SET_CONTAINER_RESOURCES:
                expected = common + (
                    ("test", f"{root}/name"),
                    ("test", f"{root}/resources"),
                    (
                        "replace"
                        if self.rollback_snapshot.resources_present
                        else "remove",
                        f"{root}/resources",
                    ),
                )
            else:
                raise ValueError("operation has no exact rollback patch shape")
            if shape != expected:
                raise ValueError("rollback patch does not match its fixed typed shape")
        return self


class LiveMutationPermit(StrictModel):
    """Short-lived binding emitted only after immediate authorization revalidation."""

    operation_id: Identifier
    plan_hash: Sha256Hex
    authorization_scope_hash: Sha256Hex
    rollback_scope_hash: Sha256Hex | None = None
    patch_bytes_hash: Sha256Hex
    direction: PatchDirection
    verifier_id: Identifier
    authorization_reference: NonEmptyStr
    environment: Identifier | None = None
    external_permit_id: Identifier | None = None
    external_claims_hash: Sha256Hex | None = None
    approval_provenance_reference: NonEmptyStr | None = None
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value.astimezone(UTC)


class MutationResult(StrictModel):
    outcome: MutationOutcome
    reason: NonEmptyStr
    response_resource_version: OpaqueKubernetesIdentity | None = None
    response_generation: int | None = Field(default=None, ge=1)


class RolloutPolicy(StrictModel):
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=60)
    stall_after_seconds: float = Field(default=120.0, gt=0, le=1800)

    @model_validator(mode="after")
    def validate_windows(self):
        if self.stall_after_seconds > self.timeout_seconds:
            raise ValueError("stall window cannot exceed rollout timeout")
        return self


class RolloutObservationResult(StrictModel):
    outcome: RolloutOutcome
    reason: NonEmptyStr
    observations: int = Field(ge=0)
    expected_generation: int = Field(ge=1)
    last_snapshot: DeploymentSnapshot | None = None


class ReconciliationResult(StrictModel):
    outcome: ReconciliationOutcome
    reason: NonEmptyStr
    snapshot: DeploymentSnapshot


class CoordinatorResult(StrictModel):
    execution_id: Identifier
    state: str
    reason: NonEmptyStr
    operator_intervention_required: bool = False


def artifact_content_hash(artifact: ValidatedPatchArtifact) -> str:
    return canonical_sha256(artifact.model_dump(mode="python"))
