"""Typed, bounded RFC 6902 patch construction with atomic test operations."""

from copy import deepcopy
from typing import Any

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import JournalConflictError
from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentSnapshot,
    JsonPatchOperation,
    PreflightDecision,
    PreflightResult,
    RollbackSnapshot,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.schemas import (
    AuthorizedOperation,
    DeployImageDigest,
    OperationType,
    ScaleReplicas,
    SetContainerResources,
)


def escape_json_pointer_token(token: str) -> str:
    """Escape one RFC 6901 path token in the required order."""
    return token.replace("~", "~0").replace("/", "~1")


def _resource_value(
    operation: SetContainerResources, prior: dict[str, Any]
) -> dict[str, Any]:
    resources = deepcopy(prior)
    requests = dict(resources.get("requests") or {})
    limits = dict(resources.get("limits") or {})
    if operation.cpu_request_millicores is not None:
        requests["cpu"] = f"{operation.cpu_request_millicores}m"
    if operation.cpu_limit_millicores is not None:
        limits["cpu"] = f"{operation.cpu_limit_millicores}m"
    if operation.memory_request_mib is not None:
        requests["memory"] = f"{operation.memory_request_mib}Mi"
    if operation.memory_limit_mib is not None:
        limits["memory"] = f"{operation.memory_limit_mib}Mi"
    if requests:
        resources["requests"] = requests
    else:
        resources.pop("requests", None)
    if limits:
        resources["limits"] = limits
    else:
        resources.pop("limits", None)
    return resources


def build_typed_patch(
    operation: AuthorizedOperation,
    snapshot: DeploymentSnapshot,
    preflight: PreflightResult,
) -> TypedPatchPreview:
    """Build one fixed-shape patch. Arbitrary paths or caller patch input are impossible."""
    if preflight.decision != PreflightDecision.PASS:
        raise JournalConflictError("typed patch requires a passing preflight result")
    if preflight.operation_id != operation.operation_id:
        raise JournalConflictError("preflight result belongs to another operation")
    if (
        preflight.snapshot_uid != snapshot.uid
        or preflight.snapshot_resource_version != snapshot.resource_version
    ):
        raise JournalConflictError("preflight result belongs to another snapshot")
    matches = snapshot.matching_containers(operation.target.container)
    if len(matches) != 1:
        raise JournalConflictError("target container is absent or ambiguous")
    container_index, container = matches[0]
    container_path = f"/spec/template/spec/containers/{container_index}"
    patch: list[JsonPatchOperation] = [
        JsonPatchOperation(op="test", path="/metadata/uid", value=snapshot.uid),
        JsonPatchOperation(
            op="test",
            path="/metadata/resourceVersion",
            value=snapshot.resource_version,
        ),
    ]
    typed_operation = operation.operation

    if isinstance(typed_operation, DeployImageDigest):
        patch.extend(
            (
                JsonPatchOperation(
                    op="test", path=f"{container_path}/name", value=container.name
                ),
                JsonPatchOperation(
                    op="test", path=f"{container_path}/image", value=container.image
                ),
                JsonPatchOperation(
                    op="replace",
                    path=f"{container_path}/image",
                    value=typed_operation.image,
                ),
            )
        )
        rollback = RollbackSnapshot(
            target_uid=snapshot.uid,
            base_resource_version=snapshot.resource_version,
            operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
            container_name=container.name,
            container_index=container_index,
            prior_image=container.image,
        )
    elif isinstance(typed_operation, ScaleReplicas):
        patch.extend(
            (
                JsonPatchOperation(
                    op="test", path="/spec/replicas", value=snapshot.replicas
                ),
                JsonPatchOperation(
                    op="replace",
                    path="/spec/replicas",
                    value=typed_operation.to_replicas,
                ),
            )
        )
        rollback = RollbackSnapshot(
            target_uid=snapshot.uid,
            base_resource_version=snapshot.resource_version,
            operation_type=OperationType.SCALE_REPLICAS,
            container_name=container.name,
            container_index=container_index,
            prior_replicas=snapshot.replicas,
        )
    elif isinstance(typed_operation, SetContainerResources):
        prior_resources = (
            deepcopy(container.raw["resources"])
            if container.resources_present
            else None
        )
        new_resources = _resource_value(typed_operation, prior_resources or {})
        patch.append(
            JsonPatchOperation(
                op="test", path=f"{container_path}/name", value=container.name
            )
        )
        if container.resources_present:
            patch.append(
                JsonPatchOperation(
                    op="test",
                    path=f"{container_path}/resources",
                    value=prior_resources,
                )
            )
            resource_op = "replace"
        else:
            resource_op = "add"
        patch.append(
            JsonPatchOperation(
                op=resource_op,
                path=f"{container_path}/resources",
                value=new_resources,
            )
        )
        rollback = RollbackSnapshot(
            target_uid=snapshot.uid,
            base_resource_version=snapshot.resource_version,
            operation_type=OperationType.SET_CONTAINER_RESOURCES,
            container_name=container.name,
            container_index=container_index,
            prior_resources=prior_resources,
            resources_present=container.resources_present,
        )
    else:
        raise JournalConflictError(
            "Phase 2 patch builder supports only image, replica, and resource operations"
        )

    payload = {
        "schema_version": "1.0",
        "operation_id": operation.operation_id,
        "target_uid": snapshot.uid,
        "base_resource_version": snapshot.resource_version,
        "content_type": "application/json-patch+json",
        "patch": tuple(patch),
        "rollback": rollback,
        "dry_run_only": True,
    }
    return TypedPatchPreview(**payload, patch_hash=canonical_sha256(payload))
