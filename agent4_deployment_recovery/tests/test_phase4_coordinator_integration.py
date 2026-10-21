from datetime import UTC, datetime, timedelta

from agent4_deployment_recovery.adapters.signed_contracts import (
    SignedAuthorizationPermitVerifier,
)
from agent4_deployment_recovery.core.coordinator import (
    EXTERNAL_AUTHORIZATION_ARTIFACT,
    ExecutionCoordinator,
)
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    ApprovalProvenanceReference,
    ExternalAuthorizationClaimsV1,
    SignedAuthorizationEnvelopeV1,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    DryRunDecision,
    ServerDryRunResult,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import build_forward_artifact
from agent4_deployment_recovery.core.schemas import ExecutionState, VerificationVerdict
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal
from agent4_deployment_recovery.tests.test_phase3_coordinator import (
    StatefulInspector,
    StatefulMutation,
    TestAuthorization,
    TestRecoveryVerifier,
)
from agent4_deployment_recovery.tests.test_phase4_signed_contracts import (
    TrustedApproval,
    _keys,
    _signature,
)


def _signed_material(operation, plan, snapshot, policy, private):
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
        reason="accepted",
    )
    artifact = build_forward_artifact(plan, snapshot, preflight, preview, dry_run)
    claims = ExternalAuthorizationClaimsV1(
        permit_id="PERMIT-coordinator-001",
        issuer="trusted-issuer",
        audience="agent4-deployment-recovery",
        issued_at=operation.issued_at,
        not_before=operation.issued_at,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        operation_id=operation.operation_id,
        correlation_id=operation.correlation_id,
        environment=operation.environment,
        target=operation.target,
        operation=operation.operation,
        plan_hash=plan.plan_hash,
        patch_hash=artifact.patch_bytes_hash,
        authorization_scope_hash=plan.authorization_scope_hash,
        rollback_scope=plan.rollback_scope,
        approval=ApprovalProvenanceReference(
            provider_id="hitl-provider",
            receipt_id="RECEIPT-coordinator-001",
            approver_subject="operator@example.test",
            method=ApprovalMethod.HUMAN_INTERACTIVE,
            evidence_hash="e" * 64,
            approved_at=operation.issued_at,
        ),
    )
    return SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )


def test_coordinator_binds_signed_permit_before_mutation(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan = create_plan(operation)
    snapshot = snapshot_factory()
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    private, registry = _keys()
    external = SignedAuthorizationPermitVerifier(
        registry, approval_verifier=TrustedApproval(), replay_store=journal
    )
    inspector = StatefulInspector(snapshot)
    mutation = StatefulMutation(inspector)
    coordinator = ExecutionCoordinator(
        journal=journal,
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        external_authorization_verifier=external,
        allowed_live_environments=frozenset({"development"}),
        mutation_adapter=mutation,
        recovery_verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
    )
    result = coordinator.execute(
        operation,
        plan,
        execution_id="EXEC-signed",
        signed_authorization=_signed_material(
            operation, plan, snapshot, preflight_policy, private
        ),
    )
    assert result.state == ExecutionState.SUCCEEDED.value
    assert len(mutation.calls) == 1
    content, _ = journal.get_artifact(
        result.execution_id, EXTERNAL_AUTHORIZATION_ARTIFACT
    )
    assert "PERMIT-coordinator-001" in content


def test_configured_external_boundary_denies_missing_signed_permit(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan = create_plan(operation)
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    _, registry = _keys()
    inspector = StatefulInspector(snapshot_factory())
    mutation = StatefulMutation(inspector)
    coordinator = ExecutionCoordinator(
        journal=journal,
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        external_authorization_verifier=SignedAuthorizationPermitVerifier(
            registry, approval_verifier=TrustedApproval(), replay_store=journal
        ),
        allowed_live_environments=frozenset({"development"}),
        mutation_adapter=mutation,
        recovery_verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
    )
    result = coordinator.execute(operation, plan, execution_id="EXEC-unsigned")
    assert result.state == ExecutionState.FAILED.value
    assert mutation.calls == []
