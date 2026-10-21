from copy import deepcopy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.core.authorization import AuthorizationDecision
from agent4_deployment_recovery.core.coordinator import ExecutionCoordinator
from agent4_deployment_recovery.core.errors import (
    AuthorizationDeniedError,
    JournalConflictError,
    KubernetesConflictError,
    KubernetesTimeoutError,
)
from agent4_deployment_recovery.core.execution_models import (
    MutationOutcome,
    MutationResult,
    PatchDirection,
    RolloutObservationResult,
    RolloutOutcome,
    ValidatedPatchArtifact,
)
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import (
    DeploymentPlan,
    ExecutionState,
    RecoveryVerification,
    VerificationVerdict,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


class TestAuthorization:
    def __init__(self, reject_on_call=None):
        self.calls = 0
        self.reject_on_call = reject_on_call

    def verify(self, operation):
        self.calls += 1
        allowed = self.calls != self.reject_on_call
        return AuthorizationDecision(
            authorized=allowed,
            verifier_id="trusted-test-authorizer",
            checked_at=datetime.now(UTC),
            reason="isolated test decision",
            authorization_reference="AUTH-isolated-test" if allowed else None,
        )


class StatefulInspector:
    def __init__(self, snapshot, *, change_rv_on_call=None, error_on_call=None):
        self.snapshot = snapshot
        self.change_rv_on_call = change_rv_on_call
        self.error_on_call = error_on_call
        self.inspect_calls = 0
        self.dry_run_calls = 0

    @property
    def cluster_identity(self):
        return self.snapshot.cluster

    def inspect_deployment(self, target):
        del target
        self.inspect_calls += 1
        if self.error_on_call == self.inspect_calls:
            raise KubernetesTimeoutError("injected inspection timeout")
        if self.change_rv_on_call == self.inspect_calls:
            self.snapshot = self.snapshot.model_copy(
                update={"resource_version": "unexpected-rv"}
            )
        return self.snapshot

    def server_side_dry_run(self, target, preview):
        del target, preview
        self.dry_run_calls += 1
        return self.snapshot


class StatefulMutation:
    def __init__(self, inspector, *, uncertain=False, apply_before_uncertain=False):
        self.inspector = inspector
        self.uncertain = uncertain
        self.apply_before_uncertain = apply_before_uncertain
        self.calls = []

    def _apply(self, artifact):
        snapshot = self.inspector.snapshot
        rollback = artifact.rollback_snapshot
        container = snapshot.containers[rollback.container_index]
        raw = deepcopy(container.raw)
        if rollback.operation_type.value == "DEPLOY_IMAGE_DIGEST":
            raw["image"] = artifact.patch[-1].value
            container = container.model_copy(
                update={"image": artifact.patch[-1].value, "raw": raw}
            )
        elif rollback.operation_type.value != "SCALE_REPLICAS":
            if artifact.patch[-1].op == "remove":
                raw.pop("resources", None)
                container = container.model_copy(
                    update={"raw": raw, "resources_present": False}
                )
            else:
                raw["resources"] = artifact.patch[-1].value
                container = container.model_copy(
                    update={"raw": raw, "resources_present": True}
                )
        replicas = (
            artifact.patch[-1].value
            if rollback.operation_type.value == "SCALE_REPLICAS"
            else snapshot.replicas
        )
        generation = snapshot.generation + 1
        self.inspector.snapshot = snapshot.model_copy(
            update={
                "containers": tuple(
                    container if index == rollback.container_index else item
                    for index, item in enumerate(snapshot.containers)
                ),
                "replicas": replicas,
                "ready_replicas": replicas,
                "updated_replicas": replicas,
                "available_replicas": replicas,
                "resource_version": str(int(snapshot.resource_version) + 1),
                "generation": generation,
                "observed_generation": generation,
            }
        )

    def apply_validated_patch(self, target, artifact, permit):
        del target
        assert permit.patch_bytes_hash == artifact.patch_bytes_hash
        self.calls.append((artifact.direction, artifact.patch_bytes_hash))
        should_apply = not self.uncertain or self.apply_before_uncertain
        if should_apply:
            self._apply(artifact)
        return MutationResult(
            outcome=(
                MutationOutcome.UNCERTAIN if self.uncertain else MutationOutcome.APPLIED
            ),
            reason="injected uncertain response" if self.uncertain else "applied",
            response_generation=(
                self.inspector.snapshot.generation if should_apply else None
            ),
        )


class FailRollbackMutation(StatefulMutation):
    def apply_validated_patch(self, target, artifact, permit):
        if artifact.direction == PatchDirection.ROLLBACK:
            raise KubernetesConflictError("injected rollback conflict")
        return super().apply_validated_patch(target, artifact, permit)


class UncertainRollbackMutation(StatefulMutation):
    def apply_validated_patch(self, target, artifact, permit):
        if artifact.direction == PatchDirection.ROLLBACK:
            self.calls.append((artifact.direction, artifact.patch_bytes_hash))
            return MutationResult(
                outcome=MutationOutcome.UNCERTAIN,
                reason="injected uncertain rollback response",
            )
        return super().apply_validated_patch(target, artifact, permit)


class SequenceRolloutObserver:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def observe(self, adapter, target, *, expected_generation, cancelled=None):
        del adapter, target, cancelled
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return RolloutObservationResult(
            outcome=outcome,
            reason=f"injected {outcome.value.lower()} rollout",
            observations=1,
            expected_generation=expected_generation,
        )


class TestRecoveryVerifier:
    def __init__(self, verdict):
        self.verdicts = list(verdict) if isinstance(verdict, tuple) else [verdict]
        self.calls = 0

    def verify(self, plan, execution):
        self.calls += 1
        return RecoveryVerification(
            verification_id=f"VERIFY-{self.calls:03d}",
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            verifier_id="trusted-agent1-test",
            verdict=self.verdicts[min(self.calls - 1, len(self.verdicts) - 1)],
            observed_at=datetime.now(UTC),
            target_uid=plan.target.uid,
            detail="isolated Agent 1 test evidence",
            signature="test-signature",
        )


def _coordinator(
    tmp_path,
    snapshot,
    policy,
    *,
    authorization=None,
    mutation=None,
    verifier=None,
    inspector=None,
    journal=None,
    observer=None,
):
    selected_inspector = inspector or StatefulInspector(snapshot)
    selected_mutation = mutation or StatefulMutation(selected_inspector)
    selected_journal = journal or SQLiteExecutionJournal(tmp_path / "journal.db")
    return (
        ExecutionCoordinator(
            journal=selected_journal,
            inspector=selected_inspector,
            policy=policy,
            authorization_verifier=authorization,
            mutation_adapter=selected_mutation,
            recovery_verifier=verifier,
            rollout_observer=observer,
        ),
        selected_inspector,
        selected_mutation,
        selected_journal,
    )


def test_success_requires_two_authorizations_and_agent1_verified(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    authorization = TestAuthorization()
    verifier = TestRecoveryVerifier(VerificationVerdict.VERIFIED)
    coordinator, inspector, mutation, journal = _coordinator(
        tmp_path,
        snapshot_factory(),
        preflight_policy,
        authorization=authorization,
        verifier=verifier,
    )
    plan = create_plan(operation)
    result = coordinator.execute(operation, plan, execution_id="EXEC-success")
    assert result.state == ExecutionState.SUCCEEDED.value
    assert authorization.calls == 2
    assert verifier.calls == 1
    assert len(mutation.calls) == 1
    assert mutation.calls[0][0] == PatchDirection.FORWARD
    assert inspector.dry_run_calls == 1
    assert journal.verify_audit_chain("EXEC-success") is True
    replay = coordinator.execute(operation, plan, execution_id="ignored")
    assert replay.state == ExecutionState.SUCCEEDED.value
    assert len(mutation.calls) == 1


def test_default_authorization_denies_before_cluster_contact(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    coordinator, inspector, mutation, _ = _coordinator(
        tmp_path, snapshot_factory(), preflight_policy
    )
    operation = operation_factory()
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-deny"
    )
    assert result.state == ExecutionState.REJECTED.value
    assert inspector.inspect_calls == 0
    assert mutation.calls == []


def test_second_authorization_rejection_and_changed_resource_version_block_mutation(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    coordinator, _, mutation, _ = _coordinator(
        tmp_path,
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(reject_on_call=2),
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-reauth"
    )
    assert result.state == ExecutionState.FAILED.value
    assert mutation.calls == []

    other_path = tmp_path / "rv"
    inspector = StatefulInspector(snapshot_factory(), change_rv_on_call=2)
    coordinator, _, mutation, _ = _coordinator(
        other_path,
        inspector.snapshot,
        preflight_policy,
        authorization=TestAuthorization(),
        inspector=inspector,
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-rv"
    )
    assert result.state == ExecutionState.FAILED.value
    assert mutation.calls == []


@pytest.mark.parametrize(
    ("apply_before_uncertain", "expected_state"),
    [
        (False, ExecutionState.FAILED.value),
        (True, ExecutionState.SUCCEEDED.value),
    ],
)
def test_uncertain_mutation_is_reconciled_without_retry(
    tmp_path,
    operation_factory,
    snapshot_factory,
    preflight_policy,
    apply_before_uncertain,
    expected_state,
):
    inspector = StatefulInspector(snapshot_factory())
    mutation = StatefulMutation(
        inspector,
        uncertain=True,
        apply_before_uncertain=apply_before_uncertain,
    )
    operation = operation_factory()
    coordinator, _, _, _ = _coordinator(
        tmp_path,
        inspector.snapshot,
        preflight_policy,
        authorization=TestAuthorization(),
        mutation=mutation,
        verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
        inspector=inspector,
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-uncertain"
    )
    assert result.state == expected_state
    assert len(mutation.calls) == 1


def test_agent1_failed_triggers_exact_rollback_and_unavailable_never_succeeds(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    coordinator, inspector, mutation, _ = _coordinator(
        tmp_path / "failed",
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
        verifier=TestRecoveryVerifier(
            (VerificationVerdict.FAILED, VerificationVerdict.VERIFIED)
        ),
    )
    prior_image = inspector.snapshot.containers[0].image
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-rollback"
    )
    assert result.state == ExecutionState.ROLLED_BACK.value
    assert [item[0] for item in mutation.calls] == [
        PatchDirection.FORWARD,
        PatchDirection.ROLLBACK,
    ]
    assert inspector.snapshot.containers[0].image == prior_image

    coordinator, _, mutation, _ = _coordinator(
        tmp_path / "unavailable",
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-unavailable"
    )
    assert result.state == ExecutionState.VERIFICATION_INCONCLUSIVE.value
    assert result.operator_intervention_required is True
    assert len(mutation.calls) == 1


def test_rollback_requires_its_own_agent1_verification_and_handles_conflict(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    coordinator, _, _, _ = _coordinator(
        tmp_path / "rollback-unavailable",
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
        verifier=TestRecoveryVerifier(
            (VerificationVerdict.FAILED, VerificationVerdict.UNAVAILABLE)
        ),
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-rb-unavailable"
    )
    assert result.state == ExecutionState.ROLLBACK_FAILED.value
    assert result.operator_intervention_required is True

    inspector = StatefulInspector(snapshot_factory())
    mutation = FailRollbackMutation(inspector)
    coordinator, _, _, _ = _coordinator(
        tmp_path / "rollback-conflict",
        inspector.snapshot,
        preflight_policy,
        authorization=TestAuthorization(),
        mutation=mutation,
        verifier=TestRecoveryVerifier(VerificationVerdict.FAILED),
        inspector=inspector,
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-rb-conflict"
    )
    assert result.state == ExecutionState.ROLLBACK_FAILED.value
    assert len(mutation.calls) == 1


def test_partial_forward_rollout_triggers_rollback_not_agent1_success(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    observer = SequenceRolloutObserver(
        (RolloutOutcome.TIMEOUT, RolloutOutcome.SUCCEEDED)
    )
    verifier = TestRecoveryVerifier(VerificationVerdict.VERIFIED)
    coordinator, _, mutation, _ = _coordinator(
        tmp_path,
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
        verifier=verifier,
        observer=observer,
    )
    result = coordinator.execute(
        operation, create_plan(operation), execution_id="EXEC-rollout-timeout"
    )
    assert result.state == ExecutionState.ROLLED_BACK.value
    assert observer.calls == 2
    assert verifier.calls == 1
    assert [direction for direction, _ in mutation.calls] == [
        PatchDirection.FORWARD,
        PatchDirection.ROLLBACK,
    ]


def test_plan_mismatch_and_journal_failure_never_reach_mutation(
    tmp_path, operation_factory, snapshot_factory, preflight_policy, monkeypatch
):
    operation = operation_factory(suffix="001")
    other_plan = create_plan(operation_factory(suffix="002"))
    coordinator, _, mutation, journal = _coordinator(
        tmp_path,
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
    )
    with pytest.raises(AuthorizationDeniedError, match="exact authorized"):
        coordinator.execute(operation, other_plan, execution_id="EXEC-mismatch")
    assert mutation.calls == []

    original = journal.record_artifact

    def fail_forward(execution_id, artifact_type, content_text, content_hash, **kwargs):
        if artifact_type == "phase3.forward_patch":
            raise RuntimeError("injected journal failure")
        return original(
            execution_id, artifact_type, content_text, content_hash, **kwargs
        )

    monkeypatch.setattr(journal, "record_artifact", fail_forward)
    with pytest.raises(RuntimeError, match="journal failure"):
        coordinator.execute(
            operation, create_plan(operation), execution_id="EXEC-journal"
        )
    assert mutation.calls == []


def test_wrong_hash_stale_plan_and_agent1_inconclusive_fail_closed(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan = create_plan(operation)
    payload = plan.model_dump(mode="python")
    payload["plan_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="plan_hash"):
        DeploymentPlan.model_validate(payload)

    inspector = StatefulInspector(snapshot_factory())
    mutation = StatefulMutation(inspector)
    stale_coordinator = ExecutionCoordinator(
        journal=SQLiteExecutionJournal(tmp_path / "stale.db"),
        inspector=inspector,
        policy=preflight_policy,
        authorization_verifier=TestAuthorization(),
        mutation_adapter=mutation,
        now=lambda: plan.expires_at,
    )
    with pytest.raises(JournalConflictError, match="expired"):
        stale_coordinator.execute(operation, plan, execution_id="EXEC-stale")
    assert mutation.calls == []

    coordinator, _, mutation, _ = _coordinator(
        tmp_path / "inconclusive",
        snapshot_factory(),
        preflight_policy,
        authorization=TestAuthorization(),
        verifier=TestRecoveryVerifier(VerificationVerdict.INCONCLUSIVE),
    )
    result = coordinator.execute(operation, plan, execution_id="EXEC-inconclusive")
    assert result.state == ExecutionState.VERIFICATION_INCONCLUSIVE.value
    assert result.operator_intervention_required is True
    assert len(mutation.calls) == 1


def test_restart_reconciles_inflight_execution_without_duplicate_mutation(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    inspector = StatefulInspector(snapshot_factory(), error_on_call=3)
    mutation = StatefulMutation(inspector, uncertain=True, apply_before_uncertain=False)
    coordinator, _, _, journal = _coordinator(
        tmp_path,
        inspector.snapshot,
        preflight_policy,
        authorization=TestAuthorization(),
        mutation=mutation,
        verifier=TestRecoveryVerifier(VerificationVerdict.VERIFIED),
        inspector=inspector,
    )
    plan = create_plan(operation)
    first = coordinator.execute(operation, plan, execution_id="EXEC-restart")
    assert first.state == ExecutionState.OUTCOME_UNCERTAIN.value
    artifact_text, _ = journal.get_artifact("EXEC-restart", "phase3.forward_patch")
    artifact = ValidatedPatchArtifact.model_validate_json(artifact_text)
    StatefulMutation(inspector)._apply(artifact)
    second = coordinator.execute(operation, plan, execution_id="ignored")
    assert second.state == ExecutionState.SUCCEEDED.value
    assert len(mutation.calls) == 1


def test_restart_completes_reconciled_rollback_without_retry(
    tmp_path, operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    inspector = StatefulInspector(snapshot_factory(), error_on_call=5)
    mutation = UncertainRollbackMutation(inspector)
    verifier = TestRecoveryVerifier(
        (VerificationVerdict.FAILED, VerificationVerdict.VERIFIED)
    )
    coordinator, _, _, journal = _coordinator(
        tmp_path,
        inspector.snapshot,
        preflight_policy,
        authorization=TestAuthorization(),
        mutation=mutation,
        verifier=verifier,
        inspector=inspector,
    )
    plan = create_plan(operation)
    first = coordinator.execute(operation, plan, execution_id="EXEC-rb-restart")
    assert first.state == ExecutionState.ROLLING_BACK.value
    rollback_text, _ = journal.get_artifact("EXEC-rb-restart", "phase3.rollback_patch")
    rollback = ValidatedPatchArtifact.model_validate_json(rollback_text)
    StatefulMutation(inspector)._apply(rollback)

    second = coordinator.execute(operation, plan, execution_id="ignored")
    assert second.state == ExecutionState.ROLLED_BACK.value
    assert [direction for direction, _ in mutation.calls] == [
        PatchDirection.FORWARD,
        PatchDirection.ROLLBACK,
    ]
