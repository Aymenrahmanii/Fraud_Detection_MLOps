"""Pure deterministic deployment-plan construction."""

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.schemas import (
    AuthorizedOperation,
    DeploymentPlan,
    NoOp,
    OperationType,
    PlanAction,
    PlanStep,
    PlanStrategy,
)


def _strategy(operation_type: OperationType) -> PlanStrategy:
    return {
        OperationType.DEPLOY_IMAGE_DIGEST: PlanStrategy.NATIVE_ROLLING,
        OperationType.SCALE_REPLICAS: PlanStrategy.DIRECT_RESOURCE_PATCH,
        OperationType.SET_CONTAINER_RESOURCES: PlanStrategy.NATIVE_ROLLING,
        OperationType.ROLLBACK_EXECUTION: PlanStrategy.EXACT_STATE_ROLLBACK,
        OperationType.NO_OP: PlanStrategy.NO_OP,
    }[operation_type]


def _steps(operation_type: OperationType) -> tuple[PlanStep, ...]:
    descriptions: list[tuple[PlanAction, str, bool]] = [
        (
            PlanAction.VALIDATE_AUTHORIZATION,
            "Verify the external credential and exact approval scope",
            False,
        ),
        (
            PlanAction.VALIDATE_TARGET,
            "Verify cluster, namespace, workload UID, container, and resourceVersion",
            False,
        ),
    ]
    if operation_type == OperationType.NO_OP:
        descriptions.append(
            (PlanAction.RECORD_NO_OP, "Record the authorized no-op", False)
        )
    else:
        descriptions.extend(
            [
                (
                    PlanAction.CAPTURE_CURRENT_STATE,
                    "Capture the exact pre-change state required for rollback",
                    False,
                ),
                (
                    PlanAction.SERVER_DRY_RUN,
                    "Validate the typed operation using Kubernetes server-side dry-run",
                    False,
                ),
                (
                    PlanAction.APPLY_TYPED_OPERATION,
                    "Apply only the approved typed operation",
                    True,
                ),
                (
                    PlanAction.OBSERVE_ROLLOUT,
                    "Observe Kubernetes rollout mechanics and bounded failure conditions",
                    False,
                ),
                (
                    PlanAction.REQUEST_AGENT1_VERIFICATION,
                    "Request independent recovery verification from a trusted Agent 1 adapter",
                    False,
                ),
            ]
        )
    return tuple(
        PlanStep(
            sequence=index,
            action=action,
            description=description,
            mutates_infrastructure=mutates,
        )
        for index, (action, description, mutates) in enumerate(descriptions, start=1)
    )


def create_plan(operation: AuthorizedOperation) -> DeploymentPlan:
    """Create a stable dry-run-only plan without reading or changing infrastructure."""
    operation.ensure_fresh()
    operation_type = OperationType(operation.operation.operation_type)
    scope_hash = operation.approval.scope_hash
    seed = canonical_sha256(
        {
            "operation_id": operation.operation_id,
            "idempotency_key": operation.idempotency_key,
            "scope_hash": scope_hash,
        }
    )
    plan_id = f"PLAN-{seed[:20].upper()}"
    preconditions = (
        f"target_uid={operation.target.uid}",
        f"expected_resource_version={operation.target.expected_resource_version}",
        f"approval_scope_hash={scope_hash}",
        "trusted_authorization_verifier_required_for_live_mutation",
        "server_side_dry_run_required_before_live_mutation",
    )
    payload = {
        "schema_version": "1.0",
        "plan_id": plan_id,
        "operation_id": operation.operation_id,
        "idempotency_key": operation.idempotency_key,
        "correlation_id": operation.correlation_id,
        "trace_id": operation.trace_id,
        "environment": operation.environment,
        "target": operation.target,
        "operation": operation.operation,
        "strategy": _strategy(operation_type),
        "created_at": operation.issued_at,
        "expires_at": operation.expires_at,
        "authorization_scope_hash": scope_hash,
        "rollback_scope": operation.approval.rollback_scope,
        "preconditions": preconditions,
        "steps": _steps(operation_type),
        "requires_agent1_verification": not isinstance(operation.operation, NoOp),
        "dry_run_only": True,
    }
    return DeploymentPlan(**payload, plan_hash=canonical_sha256(payload))
