import os
from datetime import UTC, datetime, timedelta

import pytest

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import (
    KubernetesAdapterError,
    KubernetesForbiddenError,
)
from agent4_deployment_recovery.core.execution_models import RolloutPolicy
from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    PreflightPolicy,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.rollout import RolloutObserver
from agent4_deployment_recovery.core.schemas import (
    ApprovalReceipt,
    AuthorizedOperation,
    KubernetesTarget,
    OperationType,
    RollbackField,
    RollbackScope,
    ScaleReplicas,
    authorization_scope_hash,
)
from agent4_deployment_recovery.integration.kubernetes.harness import (
    OPT_IN_ENV,
    OPT_IN_VALUE,
    KubernetesHarnessConfig,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(OPT_IN_ENV) != OPT_IN_VALUE,
    reason="requires deliberate operator opt-in and a disposable Kubernetes cluster",
)


@pytest.fixture(scope="module")
def harness():
    selected = KubernetesHarnessConfig.from_operator_environment()
    return selected, selected.adapter()


def _scale_operation(snapshot):
    now = datetime.now(UTC).replace(microsecond=0)
    target = KubernetesTarget(
        cluster_id=snapshot.cluster.cluster_id,
        namespace=snapshot.namespace,
        name=snapshot.name,
        container=snapshot.containers[0].name,
        uid=snapshot.uid,
        expected_resource_version=snapshot.resource_version,
    )
    to_replicas = snapshot.replicas + 1 if snapshot.replicas < 50 else 49
    typed = ScaleReplicas(
        operation_type=OperationType.SCALE_REPLICAS,
        from_replicas=snapshot.replicas,
        to_replicas=to_replicas,
    )
    rollback = RollbackScope(
        authorized=True,
        target_uid=target.uid,
        restore_fields=(RollbackField.REPLICAS,),
        expires_at=now + timedelta(minutes=10),
    )
    operation_id = "OP-K8S-INTEGRATION"
    receipt = ApprovalReceipt(
        approval_id="APR-K8S-INTEGRATION",
        issuer="integration-test-only",
        approver_subject="integration-operator",
        operation_id=operation_id,
        scope_hash=authorization_scope_hash(
            operation_id=operation_id,
            target=target,
            operation=typed,
            rollback_scope=rollback,
        ),
        approved_at=now,
        expires_at=now + timedelta(minutes=10),
        rollback_scope=rollback,
    )
    return AuthorizedOperation(
        operation_id=operation_id,
        idempotency_key="IDEMP-K8S-INTEGRATION",
        correlation_id="CORR-K8S-INTEGRATION",
        trace_id="TRACE-K8S-INTEGRATION",
        issuer="integration-test-only",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
        environment="integration",
        target=target,
        operation=typed,
        approval=receipt,
        authorization_token="integration-test-token",
    )


def _preview(snapshot):
    operation = _scale_operation(snapshot)
    policy = PreflightPolicy(
        allowed_targets=(
            AllowedDeploymentTarget(
                cluster_id=snapshot.cluster.cluster_id,
                namespace=snapshot.namespace,
                workload=snapshot.name,
                container=snapshot.containers[0].name,
            ),
        )
    )
    plan = create_plan(operation)
    preflight = run_preflight(
        operation, snapshot, policy, requested_strategy=plan.strategy
    )
    return operation, build_typed_patch(operation, snapshot, preflight)


def test_exact_deployment_read(harness):
    config, adapter = harness
    snapshot = adapter.inspect_deployment(config.target)
    assert snapshot.namespace == config.namespace
    assert snapshot.name == config.deployment
    assert snapshot.uid == config.target_uid


def test_admission_dry_run_never_persists(harness):
    config, adapter = harness
    before = adapter.inspect_deployment(config.target)
    operation, preview = _preview(before)
    response = adapter.server_side_dry_run(operation.target, preview)
    after = adapter.inspect_deployment(config.target)
    assert response.uid == before.uid
    assert after.resource_version == before.resource_version
    assert after.replicas == before.replicas


def test_stale_resource_version_is_rejected_by_api(harness):
    config, adapter = harness
    snapshot = adapter.inspect_deployment(config.target)
    operation, preview = _preview(snapshot)
    payload = preview.model_dump(mode="python", exclude={"patch_hash"})
    stale_version = f"stale-{snapshot.resource_version}"
    payload["base_resource_version"] = stale_version
    payload["patch"][1]["value"] = stale_version
    stale = TypedPatchPreview(**payload, patch_hash=canonical_sha256(payload))
    with pytest.raises(KubernetesAdapterError):
        adapter.server_side_dry_run(operation.target, stale)


def test_rbac_denial(harness):
    config, adapter = harness
    if config.denied_namespace is None:
        pytest.skip("set AGENT4_K8S_DENIED_NAMESPACE to exercise RBAC denial")
    denied = config.target.model_copy(update={"namespace": config.denied_namespace})
    with pytest.raises(KubernetesForbiddenError):
        adapter.inspect_deployment(denied)


def test_rollout_observation_is_bounded(harness):
    config, adapter = harness
    snapshot = adapter.inspect_deployment(config.target)
    observation = RolloutObserver(
        RolloutPolicy(
            timeout_seconds=10,
            poll_interval_seconds=1,
            stall_after_seconds=5,
        )
    ).observe(
        adapter, config.target, expected_generation=snapshot.generation
    )
    assert observation.last_snapshot is not None
