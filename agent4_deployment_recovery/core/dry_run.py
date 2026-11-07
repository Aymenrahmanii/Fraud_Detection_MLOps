"""Explicitly opted-in Kubernetes server-side dry-run orchestration."""

from agent4_deployment_recovery.adapters.kubernetes import KubernetesInspectionAdapter
from agent4_deployment_recovery.core.errors import KubernetesAdapterError
from agent4_deployment_recovery.core.kubernetes_models import (
    DryRunDecision,
    ServerDryRunResult,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.schemas import AuthorizedOperation


def server_side_dry_run(
    adapter: KubernetesInspectionAdapter,
    operation: AuthorizedOperation,
    preview: TypedPatchPreview,
    *,
    contact_cluster: bool = False,
) -> ServerDryRunResult:
    """Validate the generated patch with dryRun=All and never persist it."""
    if not contact_cluster:
        return ServerDryRunResult(
            decision=DryRunDecision.BLOCK,
            admission_accepted=False,
            operation_id=operation.operation_id,
            patch_hash=preview.patch_hash,
            reason="cluster contact requires explicit opt-in",
        )
    if preview.operation_id != operation.operation_id:
        return ServerDryRunResult(
            decision=DryRunDecision.BLOCK,
            admission_accepted=False,
            operation_id=operation.operation_id,
            patch_hash=preview.patch_hash,
            reason="patch preview belongs to another operation",
        )
    if preview.target_uid != operation.target.uid:
        return ServerDryRunResult(
            decision=DryRunDecision.BLOCK,
            admission_accepted=False,
            operation_id=operation.operation_id,
            patch_hash=preview.patch_hash,
            reason="patch preview belongs to another immutable target",
        )
    try:
        response = adapter.server_side_dry_run(operation.target, preview)
    except KubernetesAdapterError as exc:
        return ServerDryRunResult(
            decision=DryRunDecision.BLOCK,
            admission_accepted=False,
            operation_id=operation.operation_id,
            patch_hash=preview.patch_hash,
            reason=f"server-side dry-run failed: {exc}",
        )
    if response.uid != operation.target.uid:
        return ServerDryRunResult(
            decision=DryRunDecision.BLOCK,
            admission_accepted=False,
            operation_id=operation.operation_id,
            patch_hash=preview.patch_hash,
            response_uid=response.uid,
            response_resource_version=response.resource_version,
            reason="dry-run response UID is ambiguous or does not match the target",
        )
    return ServerDryRunResult(
        decision=DryRunDecision.PASS,
        admission_accepted=True,
        operation_id=operation.operation_id,
        patch_hash=preview.patch_hash,
        response_uid=response.uid,
        response_resource_version=response.resource_version,
        reason=(
            "Kubernetes admission accepted dryRun=All; application rollout success "
            "has not been evaluated"
        ),
    )
