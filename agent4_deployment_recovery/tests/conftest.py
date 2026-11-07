from datetime import UTC, datetime, timedelta

import pytest

from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    ClusterIdentity,
    ContainerSnapshot,
    DeploymentSnapshot,
    KubernetesConfigMode,
    PreflightPolicy,
    ResourceRequirementsSnapshot,
)
from agent4_deployment_recovery.core.schemas import (
    ApprovalReceipt,
    AuthorizedOperation,
    DeployImageDigest,
    KubernetesTarget,
    OperationType,
    RollbackField,
    RollbackScope,
    ScaleReplicas,
    SetContainerResources,
    authorization_scope_hash,
)


@pytest.fixture
def operation_factory():
    def build(
        *,
        suffix: str = "001",
        idempotency_key: str | None = None,
        target: KubernetesTarget | None = None,
        operation=None,
        issued_at: datetime | None = None,
        expires_at: datetime | None = None,
        rollback_expires_at: datetime | None = None,
    ) -> AuthorizedOperation:
        now = issued_at or datetime.now(UTC).replace(microsecond=0)
        expiry = expires_at or now + timedelta(hours=1)
        selected_target = target or KubernetesTarget(
            cluster_id="cluster-dev",
            namespace="default",
            name="fraud-api",
            container="fraud-api",
            uid="uid-fraud-api-001",
            expected_resource_version="12345",
        )
        selected_operation = operation or DeployImageDigest(
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            image="registry.example/fraud-api@sha256:" + "a" * 64,
        )
        rollback_field = {
            DeployImageDigest: RollbackField.IMAGE,
            ScaleReplicas: RollbackField.REPLICAS,
            SetContainerResources: RollbackField.RESOURCES,
        }.get(type(selected_operation))
        rollback = (
            RollbackScope(
                authorized=True,
                target_uid=selected_target.uid,
                restore_fields=(rollback_field,),
                expires_at=rollback_expires_at or expiry,
            )
            if rollback_field is not None
            else RollbackScope()
        )
        operation_id = f"OP-{suffix}"
        scope_hash = authorization_scope_hash(
            operation_id=operation_id,
            target=selected_target,
            operation=selected_operation,
            rollback_scope=rollback,
        )
        approval = ApprovalReceipt(
            approval_id=f"APR-{suffix}",
            issuer="test-approval-issuer",
            approver_subject="test-approver",
            operation_id=operation_id,
            scope_hash=scope_hash,
            approved_at=now,
            expires_at=expiry,
            rollback_scope=rollback,
        )
        return AuthorizedOperation(
            operation_id=operation_id,
            idempotency_key=idempotency_key or f"IDEMP-{suffix}",
            correlation_id=f"CORR-{suffix}",
            trace_id=f"TRACE-{suffix}",
            issuer="test-orchestrator",
            issued_at=now,
            expires_at=expiry,
            environment="development",
            target=selected_target,
            operation=selected_operation,
            approval=approval,
            authorization_token="x" * 24,
        )

    return build


@pytest.fixture
def snapshot_factory():
    def build(
        *,
        cluster_id: str = "cluster-dev",
        namespace: str = "default",
        name: str = "fraud-api",
        uid: str = "uid-fraud-api-001",
        resource_version: str = "12345",
        replicas: int = 2,
        image: str = "registry.example/fraud-api@sha256:" + "b" * 64,
        readiness_probe=True,
        containers: tuple[ContainerSnapshot, ...] | None = None,
        strategy_type: str = "RollingUpdate",
        generation: int = 4,
        observed_generation: int = 4,
        resources_present: bool = True,
        ready_replicas: int | None = None,
        updated_replicas: int | None = None,
        available_replicas: int | None = None,
    ) -> DeploymentSnapshot:
        resources = {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "200m", "memory": "256Mi"},
        }
        target_raw = {
            "name": "fraud-api",
            "image": image,
        }
        if readiness_probe:
            target_raw["readinessProbe"] = {
                "httpGet": {"path": "/health", "port": 8000}
            }
        if resources_present:
            target_raw["resources"] = resources
        selected_containers = containers or (
            ContainerSnapshot(
                name="fraud-api",
                image=image,
                readiness_probe=(
                    {"httpGet": {"path": "/health", "port": 8000}}
                    if readiness_probe
                    else None
                ),
                liveness_probe={"httpGet": {"path": "/health", "port": 8000}},
                resources=ResourceRequirementsSnapshot(
                    **(resources if resources_present else {})
                ),
                resources_present=resources_present,
                raw=target_raw,
            ),
        )
        return DeploymentSnapshot(
            cluster=ClusterIdentity(
                cluster_id=cluster_id,
                mode=KubernetesConfigMode.KUBECONFIG,
                context="dev-context",
                api_server="https://kubernetes.example",
            ),
            namespace=namespace,
            name=name,
            uid=uid,
            resource_version=resource_version,
            generation=generation,
            observed_generation=observed_generation,
            replicas=replicas,
            ready_replicas=replicas if ready_replicas is None else ready_replicas,
            updated_replicas=replicas if updated_replicas is None else updated_replicas,
            available_replicas=(
                replicas if available_replicas is None else available_replicas
            ),
            selector={"matchLabels": {"app": "fraud-api"}},
            pod_template={
                "metadata": {"labels": {"app": "fraud-api"}},
                "spec": {
                    "containers": [item.raw for item in selected_containers],
                },
            },
            containers=selected_containers,
            strategy_type=strategy_type,
            strategy={"type": strategy_type},
        )

    return build


@pytest.fixture
def preflight_policy():
    return PreflightPolicy(
        allowed_targets=(
            AllowedDeploymentTarget(
                cluster_id="cluster-dev",
                namespace="default",
                workload="fraud-api",
                container="fraud-api",
            ),
        )
    )
