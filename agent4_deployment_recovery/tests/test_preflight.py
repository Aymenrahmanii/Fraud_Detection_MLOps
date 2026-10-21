from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    ContainerSnapshot,
    PreflightDecision,
    PreflightPolicy,
    PreflightReasonCode,
    ResourceRequirementsSnapshot,
)
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.schemas import (
    DeployImageDigest,
    OperationType,
    ScaleReplicas,
    SetContainerResources,
)


def _codes(result):
    return {reason.code for reason in result.reasons}


def test_safe_native_rolling_image_preflight_passes(
    operation_factory, snapshot_factory, preflight_policy
):
    result = run_preflight(operation_factory(), snapshot_factory(), preflight_policy)
    assert result.decision == PreflightDecision.PASS
    assert result.capabilities.native_rolling_update is True
    assert result.capabilities.canary is False
    assert result.capabilities.blue_green is False
    assert result.capabilities.rbac_verified is False


@pytest.mark.parametrize(
    ("snapshot_kwargs", "expected_code"),
    [
        ({"cluster_id": "cluster-other"}, PreflightReasonCode.CLUSTER_MISMATCH),
        ({"namespace": "other"}, PreflightReasonCode.NAMESPACE_MISMATCH),
        ({"uid": "uid-other"}, PreflightReasonCode.TARGET_UID_MISMATCH),
        (
            {"resource_version": "99999"},
            PreflightReasonCode.RESOURCE_VERSION_MISMATCH,
        ),
    ],
)
def test_identity_mismatches_block(
    operation_factory,
    snapshot_factory,
    preflight_policy,
    snapshot_kwargs,
    expected_code,
):
    result = run_preflight(
        operation_factory(), snapshot_factory(**snapshot_kwargs), preflight_policy
    )
    assert result.decision == PreflightDecision.BLOCK
    assert expected_code in _codes(result)


def test_multiple_distinct_containers_use_exact_target(
    operation_factory, snapshot_factory, preflight_policy
):
    target = snapshot_factory().containers[0]
    sidecar = ContainerSnapshot(
        name="metrics-sidecar",
        image="registry.example/metrics@sha256:" + "c" * 64,
        readiness_probe={"exec": {"command": ["true"]}},
        resources=ResourceRequirementsSnapshot(),
        resources_present=False,
        raw={
            "name": "metrics-sidecar",
            "image": "registry.example/metrics@sha256:" + "c" * 64,
            "readinessProbe": {"exec": {"command": ["true"]}},
        },
    )
    result = run_preflight(
        operation_factory(),
        snapshot_factory(containers=(sidecar, target)),
        preflight_policy,
    )
    assert result.decision == PreflightDecision.PASS


def test_duplicate_target_container_names_block_as_ambiguous(
    operation_factory, snapshot_factory, preflight_policy
):
    target = snapshot_factory().containers[0]
    result = run_preflight(
        operation_factory(),
        snapshot_factory(containers=(target, target)),
        preflight_policy,
    )
    assert result.decision == PreflightDecision.BLOCK
    assert PreflightReasonCode.AMBIGUOUS_TARGET_CONTAINER in _codes(result)


def test_missing_readiness_probe_blocks_rollout_affecting_operation(
    operation_factory, snapshot_factory, preflight_policy
):
    result = run_preflight(
        operation_factory(), snapshot_factory(readiness_probe=False), preflight_policy
    )
    assert PreflightReasonCode.MISSING_READINESS_PROBE in _codes(result)


def test_expiry_allowlists_and_rollback_snapshot_completeness_block(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    expired = run_preflight(
        operation,
        snapshot_factory(),
        preflight_policy,
        at=operation.expires_at + timedelta(seconds=1),
    )
    assert PreflightReasonCode.OPERATION_EXPIRED in _codes(expired)

    denied_policy = PreflightPolicy(
        allowed_targets=(
            AllowedDeploymentTarget(
                cluster_id="cluster-dev",
                namespace="restricted",
                workload="fraud-api",
                container="fraud-api",
            ),
        )
    )
    denied = run_preflight(operation, snapshot_factory(), denied_policy)
    assert PreflightReasonCode.NAMESPACE_NOT_ALLOWED in _codes(denied)

    target = snapshot_factory().containers[0]
    inconsistent = target.model_copy(update={"raw": {**target.raw, "image": "other"}})
    incomplete = run_preflight(
        operation,
        snapshot_factory(containers=(inconsistent,)),
        preflight_policy,
    )
    assert PreflightReasonCode.ROLLBACK_SNAPSHOT_INCOMPLETE in _codes(incomplete)


def test_mutable_current_image_only_blocks_image_rollback_safety(
    operation_factory, snapshot_factory, preflight_policy
):
    mutable = snapshot_factory(image="registry.example/fraud-api:latest")
    image_result = run_preflight(operation_factory(), mutable, preflight_policy)
    assert PreflightReasonCode.MUTABLE_CURRENT_IMAGE in _codes(image_result)

    resources = SetContainerResources(
        operation_type=OperationType.SET_CONTAINER_RESOURCES,
        memory_limit_mib=512,
    )
    resource_result = run_preflight(
        operation_factory(operation=resources), mutable, preflight_policy
    )
    assert PreflightReasonCode.MUTABLE_CURRENT_IMAGE not in _codes(resource_result)


def test_expired_rollback_scope_and_unready_deployment_block(
    operation_factory, snapshot_factory, preflight_policy
):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    operation = operation_factory(
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=1),
        rollback_expires_at=issued_at + timedelta(minutes=5),
    )
    result = run_preflight(
        operation,
        snapshot_factory(ready_replicas=1),
        preflight_policy,
        at=issued_at + timedelta(minutes=10),
    )
    assert PreflightReasonCode.ROLLBACK_SCOPE_EXPIRED in _codes(result)
    assert PreflightReasonCode.DEPLOYMENT_NOT_READY in _codes(result)


def test_scaling_expected_state_and_resource_result_bounds_block(
    operation_factory, snapshot_factory, preflight_policy
):
    scale = ScaleReplicas(
        operation_type=OperationType.SCALE_REPLICAS,
        from_replicas=3,
        to_replicas=4,
    )
    scale_result = run_preflight(
        operation_factory(operation=scale),
        snapshot_factory(replicas=2),
        preflight_policy,
    )
    assert PreflightReasonCode.EXPECTED_REPLICAS_MISMATCH in _codes(scale_result)

    resources = SetContainerResources(
        operation_type=OperationType.SET_CONTAINER_RESOURCES,
        cpu_request_millicores=500,
    )
    resource_result = run_preflight(
        operation_factory(operation=resources), snapshot_factory(), preflight_policy
    )
    assert PreflightReasonCode.RESOURCE_REQUEST_EXCEEDS_LIMIT in _codes(resource_result)


def test_invalid_new_image_and_unsupported_progressive_strategy_fail_closed(
    operation_factory, snapshot_factory, preflight_policy
):
    with pytest.raises(ValidationError):
        DeployImageDigest(
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            image="registry.example/fraud-api:latest",
        )
    result = run_preflight(
        operation_factory(),
        snapshot_factory(),
        preflight_policy,
        requested_strategy="CANARY",
    )
    assert result.decision == PreflightDecision.BLOCK
    assert PreflightReasonCode.UNSUPPORTED_STRATEGY in _codes(result)
    assert PreflightReasonCode.TRAFFIC_ROUTING_UNKNOWN in _codes(result)


def test_repository_fraud_api_manifest_is_unsafe_for_live_rollout(
    operation_factory, snapshot_factory, preflight_policy
):
    manifest_path = Path(__file__).parents[2] / "K8s" / "fraud-api-deployment.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest_container = manifest["spec"]["template"]["spec"]["containers"][0]
    snapshot = snapshot_factory(
        image=manifest_container["image"],
        readiness_probe="readinessProbe" in manifest_container,
    )
    result = run_preflight(operation_factory(), snapshot, preflight_policy)
    assert result.decision == PreflightDecision.BLOCK
    assert {
        PreflightReasonCode.MISSING_READINESS_PROBE,
        PreflightReasonCode.MUTABLE_CURRENT_IMAGE,
    }.issubset(_codes(result))
