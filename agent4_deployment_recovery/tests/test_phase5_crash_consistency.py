import pytest

from agent4_deployment_recovery.adapters.signed_contracts import (
    SignedAuthorizationPermitVerifier,
)
from agent4_deployment_recovery.core.coordinator import ExecutionCoordinator
from agent4_deployment_recovery.core.execution_models import RolloutOutcome
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import ExecutionState, VerificationVerdict
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal
from agent4_deployment_recovery.tests.test_phase3_coordinator import (
    SequenceRolloutObserver,
    StatefulInspector,
    StatefulMutation,
    TestAuthorization,
    TestRecoveryVerifier,
)
from agent4_deployment_recovery.tests.test_phase4_coordinator_integration import (
    _signed_material,
)
from agent4_deployment_recovery.tests.test_phase4_signed_contracts import (
    TrustedApproval,
    _keys,
)


class CrashBeforeMutationInspector(StatefulInspector):
    def inspect_deployment(self, target):
        if self.inspect_calls == 1:
            self.inspect_calls += 1
            raise SystemExit("simulated process loss after permit reservation")
        return super().inspect_deployment(target)


class CrashObserver:
    def observe(self, adapter, target, *, expected_generation, cancelled=None):
        del adapter, target, expected_generation, cancelled
        raise SystemExit("simulated process loss during rollout observation")


def test_restart_reuses_atomic_permit_reservation_before_first_mutation(
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
    inspector = CrashBeforeMutationInspector(snapshot)
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
    with pytest.raises(SystemExit):
        coordinator.execute(
            operation,
            plan,
            execution_id="EXEC-crash-before-mutation",
            signed_authorization=_signed_material(
                operation, plan, snapshot, preflight_policy, private
            ),
        )
    assert journal.get_execution("EXEC-crash-before-mutation").state == (
        ExecutionState.DRY_RUN_PASSED
    )
    assert mutation.calls == []

    restarted = ExecutionCoordinator(
        journal=SQLiteExecutionJournal(journal.path),
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        external_authorization_verifier=external,
        allowed_live_environments=frozenset({"development"}),
        mutation_adapter=mutation,
        recovery_verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
    )
    result = restarted.execute(operation, plan, execution_id="ignored")
    assert result.state == ExecutionState.SUCCEEDED.value
    assert len(mutation.calls) == 1


def test_restart_during_observation_never_repeats_mutation(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan = create_plan(operation)
    inspector = StatefulInspector(snapshot_factory())
    mutation = StatefulMutation(inspector)
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    first = ExecutionCoordinator(
        journal=journal,
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        mutation_adapter=mutation,
        recovery_verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
        rollout_observer=CrashObserver(),
    )
    with pytest.raises(SystemExit):
        first.execute(operation, plan, execution_id="EXEC-observation-crash")
    assert journal.get_execution("EXEC-observation-crash").state == (
        ExecutionState.OBSERVING
    )
    assert len(mutation.calls) == 1

    restarted = ExecutionCoordinator(
        journal=SQLiteExecutionJournal(journal.path),
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        mutation_adapter=mutation,
        recovery_verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
        rollout_observer=SequenceRolloutObserver((RolloutOutcome.SUCCEEDED,)),
    )
    result = restarted.execute(operation, plan, execution_id="ignored")
    assert result.state == ExecutionState.SUCCEEDED.value
    assert len(mutation.calls) == 1

