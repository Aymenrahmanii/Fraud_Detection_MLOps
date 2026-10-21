"""Hash-bound forward artifacts, uncertain-outcome reconciliation, and exact rollback."""

import hashlib
from typing import Any

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import (
    JournalConflictError,
    UnsafeRollbackError,
)
from agent4_deployment_recovery.core.execution_models import (
    PatchDirection,
    ReconciliationOutcome,
    ReconciliationResult,
    ValidatedPatchArtifact,
    canonical_patch_bytes,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentSnapshot,
    DryRunDecision,
    JsonPatchOperation,
    PreflightDecision,
    PreflightResult,
    ServerDryRunResult,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.schemas import DeploymentPlan, OperationType


def build_forward_artifact(
    plan: DeploymentPlan,
    snapshot: DeploymentSnapshot,
    preflight: PreflightResult,
    preview: TypedPatchPreview,
    dry_run: ServerDryRunResult,
) -> ValidatedPatchArtifact:
    """Freeze the one patch body accepted by Kubernetes admission dry-run."""
    if preflight.decision != PreflightDecision.PASS:
        raise JournalConflictError("forward artifact requires passing preflight")
    if dry_run.decision != DryRunDecision.PASS or not dry_run.admission_accepted:
        raise JournalConflictError("forward artifact requires accepted server dry-run")
    if (
        preview.operation_id != plan.operation_id
        or dry_run.operation_id != plan.operation_id
    ):
        raise JournalConflictError("plan, preview, and dry-run operation IDs differ")
    if dry_run.patch_hash != preview.patch_hash:
        raise JournalConflictError("dry-run evidence belongs to another patch")
    if preview.target_uid != plan.target.uid:
        raise JournalConflictError("patch belongs to another immutable target")
    exact = canonical_patch_bytes(preview.patch)
    return ValidatedPatchArtifact(
        operation_id=plan.operation_id,
        plan_hash=plan.plan_hash,
        authorization_scope_hash=plan.authorization_scope_hash,
        preflight_hash=canonical_sha256(preflight),
        direction=PatchDirection.FORWARD,
        target_uid=preview.target_uid,
        base_resource_version=preview.base_resource_version,
        base_generation=snapshot.generation,
        patch=preview.patch,
        patch_bytes_utf8=exact.decode("utf-8"),
        patch_bytes_hash=hashlib.sha256(exact).hexdigest(),
        source_preview_hash=preview.patch_hash,
        dry_run_result_hash=canonical_sha256(dry_run),
        rollback_snapshot=preview.rollback,
    )


def _target_container(
    snapshot: DeploymentSnapshot,
    artifact: ValidatedPatchArtifact,
):
    rollback = artifact.rollback_snapshot
    if rollback.container_index >= len(snapshot.containers):
        raise UnsafeRollbackError("captured container index no longer exists")
    container = snapshot.containers[rollback.container_index]
    if container.name != rollback.container_name:
        raise UnsafeRollbackError(
            "captured container index now identifies another container"
        )
    if len(snapshot.matching_containers(rollback.container_name)) != 1:
        raise UnsafeRollbackError("target container is absent or ambiguous")
    return container


def _forward_and_prior_values(
    artifact: ValidatedPatchArtifact,
    snapshot: DeploymentSnapshot,
) -> tuple[Any, Any, bool, bool]:
    """Return current, forward, prior, and prior-presence information."""
    rollback = artifact.rollback_snapshot
    if rollback.operation_type == OperationType.SCALE_REPLICAS:
        desired = artifact.patch[-1].value
        return snapshot.replicas, desired, rollback.prior_replicas, True
    container = _target_container(snapshot, artifact)
    if rollback.operation_type == OperationType.DEPLOY_IMAGE_DIGEST:
        return container.image, artifact.patch[-1].value, rollback.prior_image, True
    if rollback.operation_type == OperationType.SET_CONTAINER_RESOURCES:
        current_present = container.resources_present
        current = container.raw.get("resources") if current_present else None
        desired = artifact.patch[-1].value
        return (
            current,
            desired,
            rollback.prior_resources,
            bool(rollback.resources_present),
        )
    raise UnsafeRollbackError("artifact operation has no exact-state recovery contract")


def reconcile_uncertain_mutation(
    artifact: ValidatedPatchArtifact,
    snapshot: DeploymentSnapshot,
) -> ReconciliationResult:
    """Classify an uncertain request without retrying or changing infrastructure."""
    if snapshot.uid != artifact.target_uid:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.DIVERGED,
            reason="target UID changed while mutation outcome was uncertain",
            snapshot=snapshot,
        )
    try:
        current, forward, prior, prior_present = _forward_and_prior_values(
            artifact, snapshot
        )
    except UnsafeRollbackError as exc:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.DIVERGED,
            reason=str(exc),
            snapshot=snapshot,
        )
    if (
        current == forward
        and snapshot.generation == artifact.base_generation + 1
        and snapshot.resource_version != artifact.base_resource_version
    ):
        return ReconciliationResult(
            outcome=ReconciliationOutcome.APPLIED,
            reason="exact affected fields match the persisted forward patch",
            snapshot=snapshot,
        )
    if (
        artifact.rollback_snapshot.operation_type
        == OperationType.SET_CONTAINER_RESOURCES
    ):
        prior_matches = (
            snapshot.containers[
                artifact.rollback_snapshot.container_index
            ].resources_present
            == prior_present
            and current == prior
        )
    else:
        prior_matches = current == prior
    unchanged_version = (
        snapshot.resource_version == artifact.base_resource_version
        and snapshot.generation == artifact.base_generation
    )
    return ReconciliationResult(
        outcome=(
            ReconciliationOutcome.NOT_APPLIED
            if prior_matches and unchanged_version
            else ReconciliationOutcome.DIVERGED
        ),
        reason=(
            "exact affected fields still match the captured pre-mutation state"
            if prior_matches and unchanged_version
            else "affected fields or resource identity changed unexpectedly"
        ),
        snapshot=snapshot,
    )


def matches_exact_prior_state(
    artifact: ValidatedPatchArtifact,
    snapshot: DeploymentSnapshot,
) -> bool:
    """Return true only when the affected field exactly matches the rollback snapshot."""
    if snapshot.uid != artifact.target_uid:
        return False
    try:
        current, _, prior, prior_present = _forward_and_prior_values(artifact, snapshot)
    except UnsafeRollbackError:
        return False
    if (
        artifact.rollback_snapshot.operation_type
        == OperationType.SET_CONTAINER_RESOURCES
    ):
        container = snapshot.containers[artifact.rollback_snapshot.container_index]
        return container.resources_present == prior_present and current == prior
    return current == prior


def build_exact_inverse_patch(
    forward: ValidatedPatchArtifact,
    current: DeploymentSnapshot,
) -> tuple[JsonPatchOperation, ...]:
    """Build a bounded inverse only when current state proves the forward result."""
    if forward.direction != PatchDirection.FORWARD:
        raise UnsafeRollbackError("rollback source is not a forward patch")
    if current.uid != forward.target_uid:
        raise UnsafeRollbackError("target UID changed; exact rollback is unsafe")
    reconciliation = reconcile_uncertain_mutation(forward, current)
    if reconciliation.outcome != ReconciliationOutcome.APPLIED:
        raise UnsafeRollbackError(
            "current affected fields do not exactly match the persisted forward values"
        )
    rollback = forward.rollback_snapshot
    patch = [
        JsonPatchOperation(op="test", path="/metadata/uid", value=current.uid),
        JsonPatchOperation(
            op="test",
            path="/metadata/resourceVersion",
            value=current.resource_version,
        ),
    ]
    if rollback.operation_type == OperationType.SCALE_REPLICAS:
        patch.extend(
            (
                JsonPatchOperation(
                    op="test", path="/spec/replicas", value=forward.patch[-1].value
                ),
                JsonPatchOperation(
                    op="replace", path="/spec/replicas", value=rollback.prior_replicas
                ),
            )
        )
    else:
        container_path = f"/spec/template/spec/containers/{rollback.container_index}"
        patch.append(
            JsonPatchOperation(
                op="test", path=f"{container_path}/name", value=rollback.container_name
            )
        )
        if rollback.operation_type == OperationType.DEPLOY_IMAGE_DIGEST:
            patch.extend(
                (
                    JsonPatchOperation(
                        op="test",
                        path=f"{container_path}/image",
                        value=forward.patch[-1].value,
                    ),
                    JsonPatchOperation(
                        op="replace",
                        path=f"{container_path}/image",
                        value=rollback.prior_image,
                    ),
                )
            )
        elif rollback.operation_type == OperationType.SET_CONTAINER_RESOURCES:
            resource_path = f"{container_path}/resources"
            patch.append(
                JsonPatchOperation(
                    op="test", path=resource_path, value=forward.patch[-1].value
                )
            )
            patch.append(
                JsonPatchOperation(
                    op="replace" if rollback.resources_present else "remove",
                    path=resource_path,
                    value=rollback.prior_resources,
                )
            )
        else:
            raise UnsafeRollbackError("operation has no exact rollback implementation")
    return tuple(patch)


def build_exact_rollback_artifact(
    plan: DeploymentPlan,
    forward: ValidatedPatchArtifact,
    current: DeploymentSnapshot,
) -> ValidatedPatchArtifact:
    """Build a guarded inverse patch only after proving the exact forward state."""
    if forward.plan_hash != plan.plan_hash or forward.operation_id != plan.operation_id:
        raise UnsafeRollbackError("rollback source belongs to another plan")
    typed_patch = build_exact_inverse_patch(forward, current)
    exact = canonical_patch_bytes(typed_patch)
    return ValidatedPatchArtifact(
        operation_id=plan.operation_id,
        plan_hash=plan.plan_hash,
        authorization_scope_hash=plan.authorization_scope_hash,
        rollback_scope_hash=canonical_sha256(plan.rollback_scope),
        preflight_hash=forward.preflight_hash,
        direction=PatchDirection.ROLLBACK,
        target_uid=current.uid,
        base_resource_version=current.resource_version,
        base_generation=current.generation,
        patch=typed_patch,
        patch_bytes_utf8=exact.decode("utf-8"),
        patch_bytes_hash=hashlib.sha256(exact).hexdigest(),
        rollback_snapshot=forward.rollback_snapshot,
    )
