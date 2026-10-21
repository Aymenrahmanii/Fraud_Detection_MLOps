from agent4_deployment_recovery.core.errors import KubernetesTimeoutError
from agent4_deployment_recovery.core.execution_models import (
    RolloutOutcome,
    RolloutPolicy,
)
from agent4_deployment_recovery.core.kubernetes_models import DeploymentCondition
from agent4_deployment_recovery.core.rollout import RolloutObserver


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class SequenceInspector:
    def __init__(self, snapshots=(), error=None):
        self.snapshots = list(snapshots)
        self.error = error
        self.calls = 0

    def inspect_deployment(self, target):
        del target
        self.calls += 1
        if self.error:
            raise self.error
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def _observer(clock):
    return RolloutObserver(
        RolloutPolicy(
            timeout_seconds=10,
            poll_interval_seconds=1,
            stall_after_seconds=3,
        ),
        clock=clock,
    )


def test_rollout_success_is_not_agent1_verification(
    operation_factory, snapshot_factory
):
    target = operation_factory().target
    snapshot = snapshot_factory(generation=5, observed_generation=5)
    result = _observer(FakeClock()).observe(
        SequenceInspector((snapshot,)), target, expected_generation=5
    )
    assert result.outcome == RolloutOutcome.SUCCEEDED
    assert "Agent 1" in result.reason


def test_rollout_stalled_failed_stale_api_error_and_cancelled(
    operation_factory, snapshot_factory
):
    target = operation_factory().target
    partial = snapshot_factory(
        generation=5,
        observed_generation=5,
        ready_replicas=1,
        updated_replicas=1,
        available_replicas=1,
    )
    stalled = _observer(FakeClock()).observe(
        SequenceInspector((partial,)), target, expected_generation=5
    )
    assert stalled.outcome == RolloutOutcome.STALLED

    failed_snapshot = partial.model_copy(
        update={
            "conditions": (
                DeploymentCondition(
                    type="Progressing",
                    status="False",
                    reason="ProgressDeadlineExceeded",
                ),
            )
        }
    )
    failed = _observer(FakeClock()).observe(
        SequenceInspector((failed_snapshot,)), target, expected_generation=5
    )
    assert failed.outcome == RolloutOutcome.FAILED

    stale = _observer(FakeClock()).observe(
        SequenceInspector((snapshot_factory(generation=4),)),
        target,
        expected_generation=5,
    )
    assert stale.outcome == RolloutOutcome.STALE

    api_error = _observer(FakeClock()).observe(
        SequenceInspector(error=KubernetesTimeoutError("timeout")),
        target,
        expected_generation=5,
    )
    assert api_error.outcome == RolloutOutcome.API_ERROR

    cancelled = _observer(FakeClock()).observe(
        SequenceInspector((partial,)),
        target,
        expected_generation=5,
        cancelled=lambda: True,
    )
    assert cancelled.outcome == RolloutOutcome.CANCELLED
    assert cancelled.observations == 0
