"""Pure fail-closed Kubernetes capability discovery and preflight checks."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from kubernetes.utils.quantity import parse_quantity

from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentCapabilities,
    DeploymentSnapshot,
    PreflightDecision,
    PreflightPolicy,
    PreflightReason,
    PreflightReasonCode,
    PreflightResult,
    RoutingCapability,
)
from agent4_deployment_recovery.core.schemas import (
    AuthorizedOperation,
    DeployImageDigest,
    NoOp,
    OperationType,
    PlanStrategy,
    RollbackExecution,
    ScaleReplicas,
    SetContainerResources,
)


def is_immutable_image_reference(image: str) -> bool:
    parts = image.split("@sha256:")
    if len(parts) != 2 or not parts[0] or len(parts[1]) != 64:
        return False
    if any(character not in "0123456789abcdef" for character in parts[1]):
        return False
    final_component = parts[0].rsplit("/", 1)[-1]
    return ":" not in final_component


def discover_capabilities(snapshot: DeploymentSnapshot) -> DeploymentCapabilities:
    native_rolling = snapshot.strategy_type == "RollingUpdate"
    supported = [PlanStrategy.DIRECT_RESOURCE_PATCH, PlanStrategy.NO_OP]
    if native_rolling:
        supported.insert(0, PlanStrategy.NATIVE_ROLLING)
    return DeploymentCapabilities(
        native_rolling_update=native_rolling,
        native_strategy=snapshot.strategy_type,
        supported_strategies=tuple(supported),
        traffic_routing=RoutingCapability.UNKNOWN,
        limitations=(
            "RBAC is not verified by Deployment inspection",
            "traffic-routing capability is unknown without an injected provider",
            "canary and blue-green delivery are unsupported in Phase 2",
            "replica ratios are never treated as weighted traffic routing",
        ),
    )


def _reason(code: PreflightReasonCode, message: str) -> PreflightReason:
    return PreflightReason(code=code, message=message)


def _quantity(value: str, *, resource: str) -> Decimal:
    parsed = Decimal(parse_quantity(value))
    if resource == "cpu":
        return parsed * 1000
    return parsed / Decimal(1024 * 1024)


def _resource_reasons(
    operation: SetContainerResources,
    requests: dict[str, str],
    limits: dict[str, str],
) -> list[PreflightReason]:
    reasons: list[PreflightReason] = []
    try:
        current_cpu_request = (
            _quantity(requests["cpu"], resource="cpu") if "cpu" in requests else None
        )
        current_cpu_limit = (
            _quantity(limits["cpu"], resource="cpu") if "cpu" in limits else None
        )
        current_memory_request = (
            _quantity(requests["memory"], resource="memory")
            if "memory" in requests
            else None
        )
        current_memory_limit = (
            _quantity(limits["memory"], resource="memory")
            if "memory" in limits
            else None
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        return [
            _reason(
                PreflightReasonCode.RESOURCE_QUANTITY_INVALID,
                f"current Kubernetes resource quantity is invalid: {exc}",
            )
        ]

    desired_cpu_request = (
        Decimal(operation.cpu_request_millicores)
        if operation.cpu_request_millicores is not None
        else current_cpu_request
    )
    desired_cpu_limit = (
        Decimal(operation.cpu_limit_millicores)
        if operation.cpu_limit_millicores is not None
        else current_cpu_limit
    )
    desired_memory_request = (
        Decimal(operation.memory_request_mib)
        if operation.memory_request_mib is not None
        else current_memory_request
    )
    desired_memory_limit = (
        Decimal(operation.memory_limit_mib)
        if operation.memory_limit_mib is not None
        else current_memory_limit
    )
    if (
        desired_cpu_request is not None
        and desired_cpu_limit is not None
        and desired_cpu_request > desired_cpu_limit
    ) or (
        desired_memory_request is not None
        and desired_memory_limit is not None
        and desired_memory_request > desired_memory_limit
    ):
        reasons.append(
            _reason(
                PreflightReasonCode.RESOURCE_REQUEST_EXCEEDS_LIMIT,
                "resulting resource request would exceed its limit",
            )
        )

    changed = any(
        (desired is not None and current is not None and desired != current)
        or (desired is not None and current is None)
        for desired, current in (
            (desired_cpu_request, current_cpu_request),
            (desired_cpu_limit, current_cpu_limit),
            (desired_memory_request, current_memory_request),
            (desired_memory_limit, current_memory_limit),
        )
    )
    if not changed:
        reasons.append(
            _reason(
                PreflightReasonCode.RESOURCE_CHANGE_EMPTY,
                "requested resource values already match the target container",
            )
        )
    return reasons


def run_preflight(
    operation: AuthorizedOperation,
    snapshot: DeploymentSnapshot,
    policy: PreflightPolicy,
    *,
    requested_strategy: PlanStrategy | str | None = None,
    at: datetime | None = None,
) -> PreflightResult:
    """Evaluate exact observed state without making optimization or policy decisions."""
    reasons: list[PreflightReason] = []
    target = operation.target
    capabilities = discover_capabilities(snapshot)
    current = at or datetime.now(UTC)
    try:
        operation.ensure_fresh(at=current)
    except ValueError as exc:
        code = (
            PreflightReasonCode.ROLLBACK_SCOPE_EXPIRED
            if "rollback authorization" in str(exc)
            else PreflightReasonCode.OPERATION_EXPIRED
        )
        reasons.append(_reason(code, str(exc)))

    if snapshot.cluster.cluster_id != target.cluster_id:
        reasons.append(
            _reason(
                PreflightReasonCode.CLUSTER_MISMATCH,
                "observed cluster does not match target",
            )
        )
    if snapshot.namespace != target.namespace:
        reasons.append(
            _reason(
                PreflightReasonCode.NAMESPACE_MISMATCH,
                "observed namespace does not match target",
            )
        )
    if snapshot.name != target.name:
        reasons.append(
            _reason(
                PreflightReasonCode.WORKLOAD_MISMATCH,
                "observed Deployment name does not match target",
            )
        )
    if snapshot.uid != target.uid:
        reasons.append(
            _reason(
                PreflightReasonCode.TARGET_UID_MISMATCH,
                "observed Deployment UID does not match immutable target identity",
            )
        )
    if snapshot.resource_version != target.expected_resource_version:
        reasons.append(
            _reason(
                PreflightReasonCode.RESOURCE_VERSION_MISMATCH,
                "observed resourceVersion does not match the authorized precondition",
            )
        )
    cluster_matches = tuple(
        item for item in policy.allowed_targets if item.cluster_id == target.cluster_id
    )
    namespace_matches = tuple(
        item for item in cluster_matches if item.namespace == target.namespace
    )
    workload_matches = tuple(
        item for item in namespace_matches if item.workload == target.name
    )
    container_matches = tuple(
        item for item in workload_matches if item.container == target.container
    )
    if not cluster_matches:
        reasons.append(
            _reason(
                PreflightReasonCode.CLUSTER_NOT_ALLOWED, "target cluster is not allowed"
            )
        )
    if not namespace_matches:
        reasons.append(
            _reason(
                PreflightReasonCode.NAMESPACE_NOT_ALLOWED,
                "target namespace is not allowed",
            )
        )
    if not workload_matches:
        reasons.append(
            _reason(
                PreflightReasonCode.WORKLOAD_NOT_ALLOWED,
                "target workload is not allowed",
            )
        )
    if not container_matches:
        reasons.append(
            _reason(
                PreflightReasonCode.CONTAINER_NOT_ALLOWED,
                "target container is not allowed",
            )
        )
    if snapshot.observed_generation < snapshot.generation:
        reasons.append(
            _reason(
                PreflightReasonCode.DEPLOYMENT_NOT_OBSERVED,
                "Deployment controller has not observed the current generation",
            )
        )
    if (
        snapshot.ready_replicas != snapshot.replicas
        or snapshot.updated_replicas != snapshot.replicas
        or snapshot.available_replicas != snapshot.replicas
    ):
        reasons.append(
            _reason(
                PreflightReasonCode.DEPLOYMENT_NOT_READY,
                "Deployment replicas are not fully ready, updated, and available",
            )
        )

    matches = snapshot.matching_containers(target.container)
    container = matches[0][1] if len(matches) == 1 else None
    if not matches:
        reasons.append(
            _reason(
                PreflightReasonCode.TARGET_CONTAINER_NOT_FOUND,
                "target container is absent from the Deployment pod template",
            )
        )
    elif len(matches) > 1:
        reasons.append(
            _reason(
                PreflightReasonCode.AMBIGUOUS_TARGET_CONTAINER,
                "target container name is not unique",
            )
        )

    strategy = requested_strategy
    if strategy is None:
        strategy = {
            OperationType.DEPLOY_IMAGE_DIGEST: PlanStrategy.NATIVE_ROLLING,
            OperationType.SCALE_REPLICAS: PlanStrategy.DIRECT_RESOURCE_PATCH,
            OperationType.SET_CONTAINER_RESOURCES: PlanStrategy.NATIVE_ROLLING,
            OperationType.ROLLBACK_EXECUTION: PlanStrategy.EXACT_STATE_ROLLBACK,
            OperationType.NO_OP: PlanStrategy.NO_OP,
        }[OperationType(operation.operation.operation_type)]
    if isinstance(strategy, str) and strategy in {"CANARY", "BLUE_GREEN"}:
        reasons.extend(
            (
                _reason(
                    PreflightReasonCode.UNSUPPORTED_STRATEGY,
                    f"{strategy} delivery is unsupported in Phase 2",
                ),
                _reason(
                    PreflightReasonCode.TRAFFIC_ROUTING_UNKNOWN,
                    "traffic-routing capability is unknown and cannot be inferred from replicas",
                ),
            )
        )
    elif strategy not in capabilities.supported_strategies:
        reasons.append(
            _reason(
                PreflightReasonCode.UNSUPPORTED_STRATEGY,
                f"strategy {strategy} is not supported by the observed Deployment",
            )
        )

    typed_operation = operation.operation
    if isinstance(typed_operation, RollbackExecution):
        reasons.append(
            _reason(
                PreflightReasonCode.UNSUPPORTED_OPERATION,
                "rollback execution is deliberately deferred beyond Phase 2",
            )
        )
    elif isinstance(typed_operation, NoOp):
        pass
    elif container is not None:
        if not container.readiness_probe:
            reasons.append(
                _reason(
                    PreflightReasonCode.MISSING_READINESS_PROBE,
                    "target container has no readiness probe",
                )
            )
        if isinstance(typed_operation, DeployImageDigest):
            if container.raw.get("image") != container.image:
                reasons.append(
                    _reason(
                        PreflightReasonCode.ROLLBACK_SNAPSHOT_INCOMPLETE,
                        "container image snapshot is internally inconsistent",
                    )
                )
            if not is_immutable_image_reference(typed_operation.image):
                reasons.append(
                    _reason(
                        PreflightReasonCode.UNSUPPORTED_OPERATION,
                        "new image is not an immutable digest reference",
                    )
                )
            if not is_immutable_image_reference(container.image):
                reasons.append(
                    _reason(
                        PreflightReasonCode.MUTABLE_CURRENT_IMAGE,
                        "current mutable image cannot form an exact rollback snapshot",
                    )
                )
            if container.image == typed_operation.image:
                reasons.append(
                    _reason(
                        PreflightReasonCode.IMAGE_ALREADY_DEPLOYED,
                        "requested image digest is already deployed",
                    )
                )
        elif isinstance(typed_operation, ScaleReplicas):
            if snapshot.replicas != typed_operation.from_replicas:
                reasons.append(
                    _reason(
                        PreflightReasonCode.EXPECTED_REPLICAS_MISMATCH,
                        "observed replicas do not match from_replicas",
                    )
                )
        elif isinstance(typed_operation, SetContainerResources):
            raw_resources_present = "resources" in container.raw
            raw_resources = container.raw.get("resources")
            expected_resources = container.resources.model_dump(mode="python")
            if raw_resources_present != container.resources_present or (
                container.resources_present
                and (
                    not isinstance(raw_resources, dict)
                    or dict(raw_resources.get("requests") or {})
                    != expected_resources["requests"]
                    or dict(raw_resources.get("limits") or {})
                    != expected_resources["limits"]
                )
            ):
                reasons.append(
                    _reason(
                        PreflightReasonCode.ROLLBACK_SNAPSHOT_INCOMPLETE,
                        "container resource snapshot is internally inconsistent",
                    )
                )
            reasons.extend(
                _resource_reasons(
                    typed_operation,
                    container.resources.requests,
                    container.resources.limits,
                )
            )

    decision = PreflightDecision.BLOCK if reasons else PreflightDecision.PASS
    return PreflightResult(
        decision=decision,
        operation_id=operation.operation_id,
        snapshot_uid=snapshot.uid,
        snapshot_resource_version=snapshot.resource_version,
        reasons=tuple(reasons),
        capabilities=capabilities,
    )
