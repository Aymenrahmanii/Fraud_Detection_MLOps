import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from agent4_deployment_recovery.core.canonical import canonical_json
from agent4_deployment_recovery.core.errors import UnsafeRollbackError
from agent4_deployment_recovery.core.execution_models import ValidatedPatchArtifact
from agent4_deployment_recovery.core.kubernetes_models import (
    DryRunDecision,
    ServerDryRunResult,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import build_forward_artifact
from agent4_deployment_recovery.core.schemas import (
    ExecutionState,
    KubernetesTarget,
    OperationType,
    RecoveryVerification,
    RollbackExecution,
    VerificationVerdict,
)
from agent4_deployment_recovery.core.standalone_rollback import (
    StandaloneRollbackPreparation,
    prepare_standalone_rollback,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def _source_execution(tmp_path, operation_factory, snapshot_factory, preflight_policy):
    operation = operation_factory()
    plan = create_plan(operation)
    prior = snapshot_factory()
    preflight = run_preflight(
        operation, prior, preflight_policy, requested_strategy=plan.strategy
    )
    preview = build_typed_patch(operation, prior, preflight)
    dry_run = ServerDryRunResult(
        decision=DryRunDecision.PASS,
        admission_accepted=True,
        operation_id=operation.operation_id,
        patch_hash=preview.patch_hash,
        response_uid=prior.uid,
        response_resource_version=prior.resource_version,
        reason="accepted",
    )
    artifact = build_forward_artifact(plan, prior, preflight, preview, dry_run)
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-source", approval_id=operation.approval.approval_id
    )
    content = canonical_json(artifact)
    journal.record_artifact(
        record.execution_id,
        "phase3.forward_patch",
        content,
        hashlib.sha256(content.encode()).hexdigest(),
    )
    for state, kwargs in (
        (ExecutionState.AUTHORIZED, {"authorization_verifier_id": "test-verifier"}),
        (ExecutionState.PREFLIGHT_PASSED, {}),
        (ExecutionState.PLANNED, {}),
        (ExecutionState.DRY_RUN_PASSED, {}),
        (ExecutionState.EXECUTING, {}),
        (ExecutionState.OBSERVING, {}),
        (ExecutionState.AWAITING_AGENT1, {}),
    ):
        record = journal.transition(
            record.execution_id, state, expected_version=record.version, **kwargs
        )
    verification = RecoveryVerification(
        verification_id="VERIFY-source",
        execution_id=record.execution_id,
        correlation_id=record.correlation_id,
        verifier_id="trusted-agent1",
        verdict=VerificationVerdict.VERIFIED,
        observed_at=datetime.now(UTC),
        target_uid=record.target.uid,
        detail="verified",
    )
    record = journal.transition(
        record.execution_id,
        ExecutionState.SUCCEEDED,
        expected_version=record.version,
        verification=verification,
    )
    applied = snapshot_factory(
        image=operation.operation.image,
        resource_version="12346",
        generation=5,
        observed_generation=5,
    )
    return journal, record, plan, applied


def test_standalone_rollback_prepares_bounded_inverse_without_execution(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    journal, source, source_plan, applied = _source_execution(
        tmp_path, operation_factory, snapshot_factory, preflight_policy
    )
    target = KubernetesTarget(
        **source.target.model_dump(exclude={"expected_resource_version"}),
        expected_resource_version=applied.resource_version,
    )
    rollback_operation = operation_factory(
        suffix="rollback",
        target=target,
        operation=RollbackExecution(
            operation_type=OperationType.ROLLBACK_EXECUTION,
            source_execution_id=source.execution_id,
            source_plan_hash=source_plan.plan_hash,
        ),
    )
    preparation = prepare_standalone_rollback(
        journal, create_plan(rollback_operation), applied
    )
    assert isinstance(preparation, StandaloneRollbackPreparation)
    assert preparation.patch[-1].value.endswith("b" * 64)
    assert preparation.requires_separate_authorization is True
    assert preparation.executable is False


def test_standalone_rollback_rejects_source_and_concurrency_mismatch(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    journal, source, _, applied = _source_execution(
        tmp_path, operation_factory, snapshot_factory, preflight_policy
    )
    target = KubernetesTarget(
        **source.target.model_dump(exclude={"expected_resource_version"}),
        expected_resource_version="unexpected-version",
    )
    rollback_operation = operation_factory(
        suffix="rollback",
        target=target,
        operation=RollbackExecution(
            operation_type=OperationType.ROLLBACK_EXECUTION,
            source_execution_id=source.execution_id,
            source_plan_hash="f" * 64,
        ),
    )
    with pytest.raises(UnsafeRollbackError, match="plan hash"):
        prepare_standalone_rollback(journal, create_plan(rollback_operation), applied)


def test_standalone_rollback_rejects_mismatched_forward_artifact(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    journal, source, source_plan, applied = _source_execution(
        tmp_path, operation_factory, snapshot_factory, preflight_policy
    )
    content, _ = journal.get_artifact(source.execution_id, "phase3.forward_patch")
    wrong = ValidatedPatchArtifact.model_validate_json(content).model_copy(
        update={"plan_hash": "f" * 64}
    )
    wrong_content = canonical_json(wrong)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DROP TRIGGER execution_artifacts_no_update")
        connection.execute(
            "UPDATE execution_artifacts SET content_text = ?, content_hash = ? "
            "WHERE execution_id = ? AND artifact_type = ?",
            (
                wrong_content,
                hashlib.sha256(wrong_content.encode()).hexdigest(),
                source.execution_id,
                "phase3.forward_patch",
            ),
        )
    target = KubernetesTarget(
        **source.target.model_dump(exclude={"expected_resource_version"}),
        expected_resource_version=applied.resource_version,
    )
    rollback_operation = operation_factory(
        suffix="rollback",
        target=target,
        operation=RollbackExecution(
            operation_type=OperationType.ROLLBACK_EXECUTION,
            source_execution_id=source.execution_id,
            source_plan_hash=source_plan.plan_hash,
        ),
    )
    with pytest.raises(UnsafeRollbackError, match="artifact"):
        prepare_standalone_rollback(journal, create_plan(rollback_operation), applied)
