from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.core.schemas import (
    ApprovalReceipt,
    DeployImageDigest,
    KubernetesTarget,
    OperationType,
    ScaleReplicas,
    SetContainerResources,
)


def test_rejects_mutable_image_tag():
    with pytest.raises(ValidationError, match="image"):
        DeployImageDigest(
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            image="registry.example/fraud-api:latest",
        )


def test_accepts_immutable_sha256_image():
    operation = DeployImageDigest(
        operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
        image="registry.example/fraud-api@sha256:" + "f" * 64,
    )
    assert operation.image.endswith("f" * 64)


def test_rejects_tag_plus_digest_reference():
    with pytest.raises(ValidationError, match="mutable tag"):
        DeployImageDigest(
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            image="registry.example/fraud-api:stable@sha256:" + "f" * 64,
        )


def test_rejects_ambiguous_target():
    with pytest.raises(ValidationError):
        KubernetesTarget(
            cluster_id="cluster-dev",
            namespace="default",
            name="fraud-api",
            container="",
            uid="uid-001",
            expected_resource_version="123",
        )


def test_rejects_replica_no_op_and_bounds():
    with pytest.raises(ValidationError, match="must change"):
        ScaleReplicas(
            operation_type=OperationType.SCALE_REPLICAS,
            from_replicas=2,
            to_replicas=2,
        )
    with pytest.raises(ValidationError):
        ScaleReplicas(
            operation_type=OperationType.SCALE_REPLICAS,
            from_replicas=2,
            to_replicas=101,
        )


def test_rejects_invalid_resource_bounds():
    with pytest.raises(ValidationError, match="at least one"):
        SetContainerResources(operation_type=OperationType.SET_CONTAINER_RESOURCES)
    with pytest.raises(ValidationError, match="CPU request"):
        SetContainerResources(
            operation_type=OperationType.SET_CONTAINER_RESOURCES,
            cpu_request_millicores=500,
            cpu_limit_millicores=250,
        )


def test_rejects_expired_approval():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="expired"):
        ApprovalReceipt(
            approval_id="APR-expired",
            issuer="test-issuer",
            approver_subject="approver",
            operation_id="OP-expired",
            scope_hash="a" * 64,
            approved_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )


def test_unknown_operation_and_unknown_fields_fail_closed(operation_factory):
    operation = operation_factory()
    payload = operation.model_dump(mode="python")
    payload["authorization_token"] = "x" * 24
    payload["operation"] = {"operation_type": "DELETE_NAMESPACE"}
    with pytest.raises(ValidationError):
        type(operation).model_validate(payload)

    target_payload = operation.target.model_dump(mode="python")
    target_payload["unexpected"] = True
    with pytest.raises(ValidationError):
        KubernetesTarget.model_validate(target_payload)


def test_approval_scope_cannot_be_reused_for_changed_target(operation_factory):
    operation = operation_factory()
    payload = operation.model_dump(mode="python")
    payload["authorization_token"] = "x" * 24
    payload["target"] = operation.target.model_copy(
        update={"expected_resource_version": "99999"}
    )
    with pytest.raises(ValidationError, match="scope_hash"):
        type(operation).model_validate(payload)
