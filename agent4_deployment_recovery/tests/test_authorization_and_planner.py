from datetime import UTC, datetime, timedelta

import pytest

from agent4_deployment_recovery.adapters.agent1_verification import (
    UnavailableRecoveryVerifier,
)
from agent4_deployment_recovery.core.authorization import (
    AuthorizationDecision,
    DenyAllAuthorizationVerifier,
    require_authorized,
)
from agent4_deployment_recovery.core.errors import AuthorizationDeniedError
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import (
    ExecutionRecord,
    NoOp,
    OperationType,
    PlanStrategy,
    RollbackExecution,
    ScaleReplicas,
    SetContainerResources,
    VerificationVerdict,
)


class TestAuthorizationVerifier:
    def verify(self, operation):
        return AuthorizationDecision(
            authorized=True,
            verifier_id="isolated-test-verifier",
            checked_at=datetime.now(UTC),
            reason=f"test authorization for {operation.operation_id}",
            authorization_reference="TEST-ONLY",
        )


def test_default_authorization_denies_live_mutation(operation_factory):
    operation = operation_factory()
    decision = DenyAllAuthorizationVerifier().verify(operation)
    assert decision.authorized is False
    with pytest.raises(AuthorizationDeniedError):
        require_authorized(operation)


def test_test_fake_can_be_explicitly_injected(operation_factory):
    decision = require_authorized(operation_factory(), TestAuthorizationVerifier())
    assert decision.authorized is True
    assert decision.verifier_id == "isolated-test-verifier"


def test_authorization_rechecks_operation_expiry_before_calling_verifier(
    operation_factory,
):
    operation = operation_factory()

    class VerifierMustNotRun:
        def verify(self, operation):
            raise AssertionError(
                f"expired operation reached verifier: {operation.operation_id}"
            )

    with pytest.raises(AuthorizationDeniedError, match="expired"):
        require_authorized(
            operation,
            VerifierMustNotRun(),
            at=operation.expires_at,
        )


def test_authorization_rejects_replayed_positive_decision(operation_factory):
    operation = operation_factory()

    class StaleDecisionVerifier:
        def verify(self, operation):
            return AuthorizationDecision(
                authorized=True,
                verifier_id="isolated-stale-verifier",
                checked_at=operation.issued_at,
                reason="previous authorization decision",
                authorization_reference="TEST-STALE",
            )

    with pytest.raises(AuthorizationDeniedError, match="stale"):
        require_authorized(
            operation,
            StaleDecisionVerifier(),
            at=operation.issued_at + timedelta(minutes=10),
        )


def test_plan_is_deterministic_dry_run_only_and_secret_free(operation_factory):
    operation = operation_factory()
    first = create_plan(operation)
    second = create_plan(operation)
    assert first == second
    assert first.plan_hash == second.plan_hash
    assert first.dry_run_only is True
    assert "authorization_token" not in first.model_dump_json()
    assert any(step.mutates_infrastructure for step in first.steps)


def test_changed_approved_intent_changes_plan_hash(operation_factory):
    first = create_plan(operation_factory(suffix="001"))
    second = create_plan(operation_factory(suffix="002"))
    assert first.plan_hash != second.plan_hash


@pytest.mark.parametrize(
    ("operation", "strategy", "needs_verification"),
    [
        (
            ScaleReplicas(
                operation_type=OperationType.SCALE_REPLICAS,
                from_replicas=2,
                to_replicas=3,
            ),
            PlanStrategy.DIRECT_RESOURCE_PATCH,
            True,
        ),
        (
            SetContainerResources(
                operation_type=OperationType.SET_CONTAINER_RESOURCES,
                memory_request_mib=128,
                memory_limit_mib=256,
            ),
            PlanStrategy.NATIVE_ROLLING,
            True,
        ),
        (
            RollbackExecution(
                operation_type=OperationType.ROLLBACK_EXECUTION,
                source_execution_id="EXEC-source",
                source_plan_hash="a" * 64,
            ),
            PlanStrategy.EXACT_STATE_ROLLBACK,
            True,
        ),
        (
            NoOp(
                operation_type=OperationType.NO_OP, reason="no safe change is required"
            ),
            PlanStrategy.NO_OP,
            False,
        ),
    ],
)
def test_all_non_image_operation_variants_plan_deterministically(
    operation_factory, operation, strategy, needs_verification
):
    plan = create_plan(operation_factory(operation=operation))
    assert plan.strategy == strategy
    assert plan.requires_agent1_verification is needs_verification


def test_unconfigured_agent1_verifier_returns_unavailable(operation_factory):
    plan = create_plan(operation_factory())
    now = datetime.now(UTC)
    record = ExecutionRecord(
        execution_id="EXEC-001",
        operation_id=plan.operation_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        idempotency_key=plan.idempotency_key,
        correlation_id=plan.correlation_id,
        trace_id=plan.trace_id,
        target=plan.target,
        operation_type=plan.operation.operation_type,
        approval_id="APR-001",
        created_at=now,
        updated_at=now,
    )
    result = UnavailableRecoveryVerifier().verify(plan, record)
    assert result.verdict == VerificationVerdict.UNAVAILABLE
    assert result.signature is None
