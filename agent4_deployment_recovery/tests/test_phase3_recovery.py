from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.core.execution_models import (
    ReconciliationOutcome,
    ValidatedPatchArtifact,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    DryRunDecision,
    ServerDryRunResult,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import (
    build_exact_rollback_artifact,
    build_forward_artifact,
    reconcile_uncertain_mutation,
)
from agent4_deployment_recovery.core.schemas import (
    OperationType,
    SetContainerResources,
)


def _forward(operation, snapshot, policy):
    plan = create_plan(operation)
    preflight = run_preflight(
        operation, snapshot, policy, requested_strategy=plan.strategy
    )
    preview = build_typed_patch(operation, snapshot, preflight)
    dry_run = ServerDryRunResult(
        decision=DryRunDecision.PASS,
        admission_accepted=True,
        operation_id=operation.operation_id,
        patch_hash=preview.patch_hash,
        response_uid=snapshot.uid,
        response_resource_version=snapshot.resource_version,
        reason="admission accepted",
    )
    return plan, build_forward_artifact(plan, snapshot, preflight, preview, dry_run)


def test_forward_artifact_binds_exact_canonical_bytes(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan, artifact = _forward(operation, snapshot_factory(), preflight_policy)
    assert artifact.plan_hash == plan.plan_hash
    assert artifact.patch_bytes_utf8.startswith('[{"op":"test"')
    payload = artifact.model_dump()
    payload["patch_bytes_utf8"] += " "
    with pytest.raises(ValidationError, match="canonical"):
        ValidatedPatchArtifact.model_validate(payload)


def test_uncertain_reconciliation_and_exact_image_rollback(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan, artifact = _forward(operation, snapshot_factory(), preflight_policy)
    prior = snapshot_factory()
    assert (
        reconcile_uncertain_mutation(artifact, prior).outcome
        == ReconciliationOutcome.NOT_APPLIED
    )
    applied = snapshot_factory(
        image=operation.operation.image,
        resource_version="12346",
        generation=5,
        observed_generation=5,
    )
    assert (
        reconcile_uncertain_mutation(artifact, applied).outcome
        == ReconciliationOutcome.APPLIED
    )
    rollback = build_exact_rollback_artifact(plan, artifact, applied)
    assert rollback.patch[1].value == "12346"
    assert rollback.patch[-2].value == operation.operation.image
    assert rollback.patch[-1].value == prior.containers[0].image

    divergent = snapshot_factory(image="registry.example/fraud-api@sha256:" + "c" * 64)
    assert (
        reconcile_uncertain_mutation(artifact, divergent).outcome
        == ReconciliationOutcome.DIVERGED
    )


def test_resource_rollback_removes_field_that_was_originally_absent(
    operation_factory, snapshot_factory, preflight_policy
):
    typed = SetContainerResources(
        operation_type=OperationType.SET_CONTAINER_RESOURCES,
        cpu_request_millicores=150,
    )
    operation = operation_factory(operation=typed)
    prior = snapshot_factory(resources_present=False)
    plan, artifact = _forward(operation, prior, preflight_policy)
    raw = deepcopy(prior.containers[0].raw)
    raw["resources"] = artifact.patch[-1].value
    container = prior.containers[0].model_copy(
        update={"raw": raw, "resources_present": True}
    )
    applied = prior.model_copy(
        update={
            "containers": (container,),
            "resource_version": "12346",
            "generation": 5,
            "observed_generation": 5,
        }
    )
    rollback = build_exact_rollback_artifact(plan, artifact, applied)
    assert rollback.patch[-1].op == "remove"
    assert "value" not in rollback.patch[-1].model_dump(exclude_none=True)
