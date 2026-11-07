"""Strict Phase 2 contracts for Kubernetes inspection and server dry-run."""

import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.schemas import (
    ContainerName,
    Identifier,
    KubernetesName,
    NonEmptyStr,
    OpaqueKubernetesIdentity,
    OperationType,
    PlanStrategy,
    Sha256Hex,
    StrictModel,
)


class KubernetesConfigMode(str, Enum):
    KUBECONFIG = "KUBECONFIG"
    IN_CLUSTER = "IN_CLUSTER"


class KubernetesConnectionConfig(StrictModel):
    mode: KubernetesConfigMode
    cluster_id: Identifier
    kubeconfig_path: Path | None = None
    context: NonEmptyStr | None = None
    connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def require_explicit_source(self):
        if self.mode == KubernetesConfigMode.KUBECONFIG:
            if self.kubeconfig_path is None or self.context is None:
                raise ValueError(
                    "KUBECONFIG mode requires an explicit path and context"
                )
        elif self.kubeconfig_path is not None or self.context is not None:
            raise ValueError(
                "IN_CLUSTER mode cannot contain kubeconfig path or context"
            )
        return self

    @property
    def request_timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_seconds, self.read_timeout_seconds)


class ClusterIdentity(StrictModel):
    cluster_id: Identifier
    mode: KubernetesConfigMode
    context: NonEmptyStr | None = None
    api_server: NonEmptyStr


class ResourceRequirementsSnapshot(StrictModel):
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)


class ContainerSnapshot(StrictModel):
    name: ContainerName
    image: NonEmptyStr
    readiness_probe: dict[str, Any] | None = None
    liveness_probe: dict[str, Any] | None = None
    resources: ResourceRequirementsSnapshot
    resources_present: bool
    raw: dict[str, Any]


class DeploymentCondition(StrictModel):
    type: NonEmptyStr
    status: NonEmptyStr
    reason: str | None = None
    message: str | None = None
    last_transition_time: datetime | None = None


class DeploymentSnapshot(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    cluster: ClusterIdentity
    namespace: KubernetesName
    name: KubernetesName
    uid: OpaqueKubernetesIdentity
    resource_version: OpaqueKubernetesIdentity
    generation: int = Field(ge=1)
    observed_generation: int = Field(ge=0)
    replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)
    updated_replicas: int = Field(ge=0)
    available_replicas: int = Field(ge=0)
    selector: dict[str, Any]
    pod_template: dict[str, Any]
    containers: tuple[ContainerSnapshot, ...]
    strategy_type: NonEmptyStr
    strategy: dict[str, Any]
    conditions: tuple[DeploymentCondition, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self):
        if not self.selector:
            raise ValueError("deployment selector is missing")
        if not self.pod_template:
            raise ValueError("deployment pod template is missing")
        if not self.containers:
            raise ValueError("deployment has no containers")
        return self

    def matching_containers(
        self, name: str
    ) -> tuple[tuple[int, ContainerSnapshot], ...]:
        return tuple(
            (index, container)
            for index, container in enumerate(self.containers)
            if container.name == name
        )


class RoutingCapability(str, Enum):
    UNKNOWN = "UNKNOWN"


class DeploymentCapabilities(StrictModel):
    native_rolling_update: bool
    native_strategy: NonEmptyStr
    canary: Literal[False] = False
    blue_green: Literal[False] = False
    traffic_routing: Literal[RoutingCapability.UNKNOWN] = RoutingCapability.UNKNOWN
    rbac_verified: Literal[False] = False
    supported_strategies: tuple[PlanStrategy, ...]
    limitations: tuple[NonEmptyStr, ...]


class PreflightDecision(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"


class PreflightReasonCode(str, Enum):
    CLUSTER_MISMATCH = "CLUSTER_MISMATCH"
    NAMESPACE_MISMATCH = "NAMESPACE_MISMATCH"
    WORKLOAD_MISMATCH = "WORKLOAD_MISMATCH"
    TARGET_UID_MISMATCH = "TARGET_UID_MISMATCH"
    RESOURCE_VERSION_MISMATCH = "RESOURCE_VERSION_MISMATCH"
    CLUSTER_NOT_ALLOWED = "CLUSTER_NOT_ALLOWED"
    NAMESPACE_NOT_ALLOWED = "NAMESPACE_NOT_ALLOWED"
    WORKLOAD_NOT_ALLOWED = "WORKLOAD_NOT_ALLOWED"
    CONTAINER_NOT_ALLOWED = "CONTAINER_NOT_ALLOWED"
    TARGET_CONTAINER_NOT_FOUND = "TARGET_CONTAINER_NOT_FOUND"
    AMBIGUOUS_TARGET_CONTAINER = "AMBIGUOUS_TARGET_CONTAINER"
    OPERATION_EXPIRED = "OPERATION_EXPIRED"
    DEPLOYMENT_NOT_OBSERVED = "DEPLOYMENT_NOT_OBSERVED"
    DEPLOYMENT_NOT_READY = "DEPLOYMENT_NOT_READY"
    MISSING_READINESS_PROBE = "MISSING_READINESS_PROBE"
    MUTABLE_CURRENT_IMAGE = "MUTABLE_CURRENT_IMAGE"
    IMAGE_ALREADY_DEPLOYED = "IMAGE_ALREADY_DEPLOYED"
    EXPECTED_REPLICAS_MISMATCH = "EXPECTED_REPLICAS_MISMATCH"
    RESOURCE_QUANTITY_INVALID = "RESOURCE_QUANTITY_INVALID"
    RESOURCE_REQUEST_EXCEEDS_LIMIT = "RESOURCE_REQUEST_EXCEEDS_LIMIT"
    RESOURCE_CHANGE_EMPTY = "RESOURCE_CHANGE_EMPTY"
    ROLLBACK_SNAPSHOT_INCOMPLETE = "ROLLBACK_SNAPSHOT_INCOMPLETE"
    ROLLBACK_SCOPE_EXPIRED = "ROLLBACK_SCOPE_EXPIRED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    UNSUPPORTED_STRATEGY = "UNSUPPORTED_STRATEGY"
    TRAFFIC_ROUTING_UNKNOWN = "TRAFFIC_ROUTING_UNKNOWN"


class PreflightReason(StrictModel):
    code: PreflightReasonCode
    message: NonEmptyStr


class AllowedDeploymentTarget(StrictModel):
    cluster_id: Identifier
    namespace: KubernetesName
    workload: KubernetesName
    container: ContainerName


class PreflightPolicy(StrictModel):
    allowed_targets: tuple[AllowedDeploymentTarget, ...]

    @model_validator(mode="after")
    def require_nonempty_allowlists(self):
        if not self.allowed_targets:
            raise ValueError("allowed_targets must be explicit and non-empty")
        if len(set(self.allowed_targets)) != len(self.allowed_targets):
            raise ValueError("allowed_targets must be unique")
        return self


class PreflightResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    decision: PreflightDecision
    operation_id: Identifier
    snapshot_uid: OpaqueKubernetesIdentity
    snapshot_resource_version: OpaqueKubernetesIdentity
    reasons: tuple[PreflightReason, ...]
    capabilities: DeploymentCapabilities

    @model_validator(mode="after")
    def decision_matches_reasons(self):
        if self.decision == PreflightDecision.PASS and self.reasons:
            raise ValueError("PASS cannot contain blocking reasons")
        if self.decision == PreflightDecision.BLOCK and not self.reasons:
            raise ValueError("BLOCK requires at least one reason")
        return self


class JsonPatchOperation(StrictModel):
    op: Literal["test", "add", "replace", "remove"]
    path: NonEmptyStr
    value: Any = None

    @model_validator(mode="after")
    def validate_value(self):
        if self.op == "remove" and self.value is not None:
            raise ValueError("remove operations cannot contain a value")
        if self.op != "remove" and self.value is None:
            raise ValueError(f"{self.op} operations require a value")
        return self

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        exact = {
            "/metadata/uid",
            "/metadata/resourceVersion",
            "/spec/replicas",
        }
        container_path = re.fullmatch(
            r"/spec/template/spec/containers/[0-9]+(?:/(?:name|image|resources))?",
            value,
        )
        if value not in exact and container_path is None:
            raise ValueError(
                "JSON Patch path is outside the typed Deployment allowlist"
            )
        return value


class RollbackSnapshot(StrictModel):
    target_uid: OpaqueKubernetesIdentity
    base_resource_version: OpaqueKubernetesIdentity
    operation_type: OperationType
    container_name: ContainerName
    container_index: int = Field(ge=0)
    prior_image: str | None = None
    prior_replicas: int | None = Field(default=None, ge=0)
    prior_resources: dict[str, Any] | None = None
    resources_present: bool | None = None

    @model_validator(mode="after")
    def require_exact_prior_value(self):
        if self.operation_type == OperationType.DEPLOY_IMAGE_DIGEST:
            if self.prior_image is None:
                raise ValueError("image rollback snapshot requires prior_image")
        elif self.operation_type == OperationType.SCALE_REPLICAS:
            if self.prior_replicas is None:
                raise ValueError("scale rollback snapshot requires prior_replicas")
        elif self.operation_type == OperationType.SET_CONTAINER_RESOURCES:
            if self.resources_present is None:
                raise ValueError(
                    "resource rollback snapshot requires presence metadata"
                )
            if self.resources_present and self.prior_resources is None:
                raise ValueError("present resources require exact prior_resources")
            if not self.resources_present and self.prior_resources is not None:
                raise ValueError(
                    "absent resources must be represented by null prior_resources"
                )
        return self


class TypedPatchPreview(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: Identifier
    target_uid: OpaqueKubernetesIdentity
    base_resource_version: OpaqueKubernetesIdentity
    content_type: Literal["application/json-patch+json"] = "application/json-patch+json"
    patch: tuple[JsonPatchOperation, ...]
    rollback: RollbackSnapshot
    dry_run_only: Literal[True] = True
    patch_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_hash(self):
        index = self.rollback.container_index
        container_path = f"/spec/template/spec/containers/{index}"
        common = (
            ("test", "/metadata/uid"),
            ("test", "/metadata/resourceVersion"),
        )
        shape = tuple((item.op, item.path) for item in self.patch)
        if len(self.patch) < 2:
            raise ValueError("patch is missing mandatory metadata preconditions")
        if shape[:2] != common:
            raise ValueError("patch must begin with UID and resourceVersion tests")
        if self.patch[0].value != self.target_uid:
            raise ValueError("UID test does not match patch target")
        if self.patch[1].value != self.base_resource_version:
            raise ValueError("resourceVersion test does not match patch base")
        if self.rollback.operation_type == OperationType.DEPLOY_IMAGE_DIGEST:
            expected_shape = common + (
                ("test", f"{container_path}/name"),
                ("test", f"{container_path}/image"),
                ("replace", f"{container_path}/image"),
            )
            if shape != expected_shape:
                raise ValueError("image operation has an invalid patch shape")
            if self.patch[2].value != self.rollback.container_name:
                raise ValueError("container name test does not match rollback snapshot")
            if self.patch[3].value != self.rollback.prior_image:
                raise ValueError("prior image test does not match rollback snapshot")
        elif self.rollback.operation_type == OperationType.SCALE_REPLICAS:
            expected_shape = common + (
                ("test", "/spec/replicas"),
                ("replace", "/spec/replicas"),
            )
            if shape != expected_shape:
                raise ValueError("scale operation has an invalid patch shape")
            if self.patch[2].value != self.rollback.prior_replicas:
                raise ValueError("prior replica test does not match rollback snapshot")
        elif self.rollback.operation_type == OperationType.SET_CONTAINER_RESOURCES:
            if len(shape) not in {4, 5} or shape[:3] != common + (
                ("test", f"{container_path}/name"),
            ):
                raise ValueError("resource operation has an invalid patch shape")
            if self.patch[2].value != self.rollback.container_name:
                raise ValueError("container name test does not match rollback snapshot")
            if self.rollback.resources_present:
                if len(self.patch) != 5:
                    raise ValueError(
                        "resource replacement is missing a prior-value test"
                    )
                prior_test = self.patch[3]
                replacement = self.patch[4]
                if (
                    (prior_test.op, prior_test.path)
                    != ("test", f"{container_path}/resources")
                    or prior_test.value != self.rollback.prior_resources
                    or (replacement.op, replacement.path)
                    != ("replace", f"{container_path}/resources")
                ):
                    raise ValueError("resource replacement precondition is invalid")
            else:
                if len(self.patch) != 4:
                    raise ValueError(
                        "resource addition has an unexpected prior-value test"
                    )
                replacement = self.patch[3]
                if (replacement.op, replacement.path) != (
                    "add",
                    f"{container_path}/resources",
                ):
                    raise ValueError("resource addition precondition is invalid")
            if not isinstance(replacement.value, dict) or not set(
                replacement.value
            ).issubset({"requests", "limits", "claims"}):
                raise ValueError("resource patch value contains unsupported fields")
        else:
            raise ValueError(
                "operation type is unsupported by the Phase 2 patch contract"
            )
        expected = canonical_sha256(
            self.model_dump(mode="python", exclude={"patch_hash"})
        )
        if self.patch_hash != expected:
            raise ValueError("patch_hash does not match typed patch content")
        return self


class DryRunDecision(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"


class ServerDryRunResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    decision: DryRunDecision
    admission_accepted: bool
    rollout_verified: Literal[False] = False
    persisted: Literal[False] = False
    operation_id: Identifier
    patch_hash: Sha256Hex
    response_uid: OpaqueKubernetesIdentity | None = None
    response_resource_version: OpaqueKubernetesIdentity | None = None
    reason: NonEmptyStr
