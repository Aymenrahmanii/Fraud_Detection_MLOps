"""Bounded Kubernetes Deployment rollout observation."""

import time
from collections.abc import Callable
from typing import Protocol

from agent4_deployment_recovery.adapters.kubernetes import KubernetesInspectionAdapter
from agent4_deployment_recovery.core.errors import KubernetesAdapterError
from agent4_deployment_recovery.core.execution_models import (
    RolloutObservationResult,
    RolloutOutcome,
    RolloutPolicy,
)
from agent4_deployment_recovery.core.schemas import KubernetesTarget


class ObservationClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemObservationClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class RolloutObserver:
    def __init__(
        self,
        policy: RolloutPolicy | None = None,
        *,
        clock: ObservationClock | None = None,
    ):
        self._policy = policy or RolloutPolicy()
        self._clock = clock or SystemObservationClock()

    def observe(
        self,
        adapter: KubernetesInspectionAdapter,
        target: KubernetesTarget,
        *,
        expected_generation: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> RolloutObservationResult:
        """Poll status only; this never substitutes for Agent 1 verification."""
        is_cancelled = cancelled or (lambda: False)
        started = self._clock.monotonic()
        last_progress_at = started
        last_signature: tuple[int, int, int, int, int] | None = None
        last_snapshot = None
        observations = 0
        while True:
            if is_cancelled():
                return RolloutObservationResult(
                    outcome=RolloutOutcome.CANCELLED,
                    reason="rollout observation was cancelled",
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=last_snapshot,
                )
            try:
                snapshot = adapter.inspect_deployment(target)
            except KubernetesAdapterError as exc:
                return RolloutObservationResult(
                    outcome=RolloutOutcome.API_ERROR,
                    reason=f"rollout observation failed: {exc}",
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=last_snapshot,
                )
            observations += 1
            last_snapshot = snapshot
            if snapshot.uid != target.uid or snapshot.generation != expected_generation:
                return RolloutObservationResult(
                    outcome=RolloutOutcome.STALE,
                    reason="observed Deployment identity or generation differs from expectation",
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=snapshot,
                )
            failed = next(
                (
                    condition
                    for condition in snapshot.conditions
                    if (
                        condition.type == "ReplicaFailure"
                        and condition.status.lower() == "true"
                    )
                    or (
                        condition.type == "Progressing"
                        and condition.status.lower() == "false"
                        and condition.reason == "ProgressDeadlineExceeded"
                    )
                ),
                None,
            )
            if failed is not None:
                return RolloutObservationResult(
                    outcome=RolloutOutcome.FAILED,
                    reason=(
                        f"Deployment condition {failed.type} reported {failed.reason or 'failure'}"
                    ),
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=snapshot,
                )
            desired = snapshot.replicas
            if (
                snapshot.observed_generation >= expected_generation
                and snapshot.updated_replicas == desired
                and snapshot.ready_replicas == desired
                and snapshot.available_replicas == desired
            ):
                return RolloutObservationResult(
                    outcome=RolloutOutcome.SUCCEEDED,
                    reason=(
                        "Kubernetes rollout mechanics converged; independent Agent 1 "
                        "verification is still required"
                    ),
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=snapshot,
                )
            now = self._clock.monotonic()
            signature = (
                snapshot.generation,
                snapshot.observed_generation,
                snapshot.updated_replicas,
                snapshot.ready_replicas,
                snapshot.available_replicas,
            )
            if signature != last_signature:
                last_signature = signature
                last_progress_at = now
            elif now - last_progress_at >= self._policy.stall_after_seconds:
                return RolloutObservationResult(
                    outcome=RolloutOutcome.STALLED,
                    reason="Deployment made no observable progress within the stall window",
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=snapshot,
                )
            elapsed = now - started
            if elapsed >= self._policy.timeout_seconds:
                return RolloutObservationResult(
                    outcome=RolloutOutcome.TIMEOUT,
                    reason="Deployment did not converge before the observation deadline",
                    observations=observations,
                    expected_generation=expected_generation,
                    last_snapshot=snapshot,
                )
            self._clock.sleep(
                min(
                    self._policy.poll_interval_seconds,
                    self._policy.timeout_seconds - elapsed,
                )
            )
