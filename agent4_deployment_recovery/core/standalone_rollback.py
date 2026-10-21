"""Read-only preparation of a separately authorized standalone rollback."""

from typing import Literal

from pydantic import Field

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.errors import UnsafeRollbackError
from agent4_deployment_recovery.core.execution_models import (
    PatchDirection,
    ValidatedPatchArtifact,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentSnapshot,
    JsonPatchOperation,
    RollbackSnapshot,
)
from agent4_deployment_recovery.core.recovery import build_exact_inverse_patch
from agent4_deployment_recovery.core.schemas import (
    DeploymentPlan,
    ExecutionState,
    Identifier,
    RollbackExecution,
    Sha256Hex,
    StrictModel,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal

FORWARD_ARTIFACT = "phase3.forward_patch"


class StandaloneRollbackPreparation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    source_execution_id: Identifier
    source_operation_id: Identifier
    source_plan_hash: Sha256Hex
    source_artifact_hash: Sha256Hex
    rollback_plan_hash: Sha256Hex
    target_uid: str = Field(min_length=1, max_length=256)
    base_resource_version: str = Field(min_length=1, max_length=256)
    patch: tuple[JsonPatchOperation, ...]
    patch_hash: Sha256Hex
    rollback_snapshot: RollbackSnapshot
    requires_separate_authorization: Literal[True] = True
    executable: Literal[False] = False


def prepare_standalone_rollback(
    journal: SQLiteExecutionJournal,
    rollback_plan: DeploymentPlan,
    current: DeploymentSnapshot,
) -> StandaloneRollbackPreparation:
    """Validate prior evidence and return an inverse preview without mutation."""
    operation = rollback_plan.operation
    if not isinstance(operation, RollbackExecution):
        raise UnsafeRollbackError("standalone rollback requires ROLLBACK_EXECUTION")
    try:
        source = journal.get_execution(operation.source_execution_id)
        content, artifact_hash = journal.get_artifact(
            source.execution_id, FORWARD_ARTIFACT
        )
    except KeyError as exc:
        raise UnsafeRollbackError(
            "source execution or forward artifact is unavailable"
        ) from exc
    if source.state not in {
        ExecutionState.SUCCEEDED,
        ExecutionState.VERIFICATION_INCONCLUSIVE,
    }:
        raise UnsafeRollbackError("source execution is not in a recoverable state")
    if source.plan_hash != operation.source_plan_hash:
        raise UnsafeRollbackError("source plan hash does not match rollback request")
    source_identity = (
        source.target.cluster_id,
        source.target.namespace,
        source.target.name,
        source.target.container,
        source.target.uid,
    )
    rollback_identity = (
        rollback_plan.target.cluster_id,
        rollback_plan.target.namespace,
        rollback_plan.target.name,
        rollback_plan.target.container,
        rollback_plan.target.uid,
    )
    if source_identity != rollback_identity:
        raise UnsafeRollbackError("rollback target is not the exact source target")
    if current.uid != rollback_plan.target.uid:
        raise UnsafeRollbackError(
            "current Deployment UID does not match rollback target"
        )
    if current.resource_version != rollback_plan.target.expected_resource_version:
        raise UnsafeRollbackError(
            "current resourceVersion differs from rollback precondition"
        )
    forward = ValidatedPatchArtifact.model_validate_json(content)
    if (
        forward.direction != PatchDirection.FORWARD
        or forward.plan_hash != source.plan_hash
        or forward.operation_id != source.operation_id
        or forward.target_uid != source.target.uid
    ):
        raise UnsafeRollbackError("forward artifact does not match source execution")
    patch = build_exact_inverse_patch(forward, current)
    return StandaloneRollbackPreparation(
        source_execution_id=source.execution_id,
        source_operation_id=source.operation_id,
        source_plan_hash=source.plan_hash,
        source_artifact_hash=artifact_hash,
        rollback_plan_hash=rollback_plan.plan_hash,
        target_uid=current.uid,
        base_resource_version=current.resource_version,
        patch=patch,
        patch_hash=canonical_sha256(
            [item.model_dump(mode="python", exclude_none=True) for item in patch]
        ),
        rollback_snapshot=forward.rollback_snapshot,
    )
