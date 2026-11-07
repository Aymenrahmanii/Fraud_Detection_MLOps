"""Offline Agent 4 scenarios using production coordination and explicit fakes.

Nothing in this module loads Kubernetes configuration or contacts a cluster.
The fake authorization, inspection, mutation, rollout, and Agent 1 boundaries
exist only to make the production coordinator observable on a developer laptop.
"""

import argparse
import json
import tempfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent4_deployment_recovery.core.authorization import AuthorizationDecision
from agent4_deployment_recovery.core.coordinator import ExecutionCoordinator
from agent4_deployment_recovery.core.execution_models import (
    MutationOutcome,
    MutationResult,
    RolloutObservationResult,
    RolloutOutcome,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    ClusterIdentity,
    ContainerSnapshot,
    DeploymentSnapshot,
    KubernetesConfigMode,
    PreflightPolicy,
    ResourceRequirementsSnapshot,
)
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import (
    ApprovalReceipt,
    AuthorizedOperation,
    DeployImageDigest,
    KubernetesTarget,
    OperationType,
    RecoveryVerification,
    RollbackField,
    RollbackScope,
    VerificationVerdict,
    authorization_scope_hash,
)
from agent4_deployment_recovery.persistence.sqlite_journal import (
    SQLiteExecutionJournal,
)


class DemoAuthorizationVerifier:
    """Explicit demo-only trust boundary; never used by the runtime factory."""

    def __init__(self, clock):
        self._clock = clock

    def verify(self, operation):
        del operation
        return AuthorizationDecision(
            authorized=True,
            verifier_id="offline-demo-authorizer",
            checked_at=self._clock(),
            reason="explicit offline demonstration authorization",
            authorization_reference="offline-demo-approval",
        )


class DemoInspector:
    """In-memory exact Deployment snapshot and server dry-run simulation."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.dry_run_requests = 0

    @property
    def cluster_identity(self):
        return self.snapshot.cluster

    def inspect_deployment(self, target):
        del target
        return self.snapshot

    def server_side_dry_run(self, target, preview):
        del target, preview
        self.dry_run_requests += 1
        return self.snapshot


class DemoMutationAdapter:
    """In-memory simulation accepting only coordinator-produced artifacts."""

    def __init__(self, inspector):
        self.inspector = inspector
        self.calls = []

    def apply_validated_patch(self, target, artifact, permit):
        del target
        if permit.patch_bytes_hash != artifact.patch_bytes_hash:
            raise ValueError("demo permit does not bind the exact patch")
        self.calls.append(artifact.direction)
        snapshot = self.inspector.snapshot
        rollback = artifact.rollback_snapshot
        container = snapshot.containers[rollback.container_index]
        raw = deepcopy(container.raw)
        raw["image"] = artifact.patch[-1].value
        container = container.model_copy(
            update={"image": artifact.patch[-1].value, "raw": raw}
        )
        generation = snapshot.generation + 1
        self.inspector.snapshot = snapshot.model_copy(
            update={
                "containers": (container,),
                "resource_version": str(int(snapshot.resource_version) + 1),
                "generation": generation,
                "observed_generation": generation,
            }
        )
        return MutationResult(
            outcome=MutationOutcome.APPLIED,
            reason="offline in-memory mutation applied",
            response_generation=generation,
        )


class DemoRolloutObserver:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    def observe(self, adapter, target, *, expected_generation, cancelled=None):
        del adapter, target, cancelled
        outcome = self._outcomes.pop(0)
        return RolloutObservationResult(
            outcome=outcome,
            reason=f"offline {outcome.value.lower()} rollout",
            observations=1,
            expected_generation=expected_generation,
        )


class CrashOnceObserver:
    def observe(self, adapter, target, *, expected_generation, cancelled=None):
        del adapter, target, expected_generation, cancelled
        raise RuntimeError("simulated process interruption after mutation")


class DemoRecoveryVerifier:
    """Explicit fake standing in for a future trusted Agent 1 verifier."""

    def __init__(self, clock, verdicts):
        self._clock = clock
        self._verdicts = list(verdicts)
        self._calls = 0

    def verify(self, plan, execution):
        self._calls += 1
        verdict = self._verdicts.pop(0)
        return RecoveryVerification(
            verification_id=f"DEMO-VERIFY-{self._calls:03d}",
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            verifier_id="offline-demo-agent1",
            verdict=verdict,
            observed_at=self._clock(),
            target_uid=plan.target.uid,
            detail="synthetic offline evidence, not production Agent 1 evidence",
            signature="offline-demo-signature",
        )


def _snapshot():
    old_image = "registry.example/fraud-api@sha256:" + ("b" * 64)
    resources = {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "200m", "memory": "256Mi"},
    }
    readiness = {"httpGet": {"path": "/health", "port": 8000}}
    raw = {
        "name": "fraud-api",
        "image": old_image,
        "readinessProbe": readiness,
        "livenessProbe": readiness,
        "resources": resources,
    }
    container = ContainerSnapshot(
        name="fraud-api",
        image=old_image,
        readiness_probe=readiness,
        liveness_probe=readiness,
        resources=ResourceRequirementsSnapshot(**resources),
        resources_present=True,
        raw=raw,
    )
    return DeploymentSnapshot(
        cluster=ClusterIdentity(
            cluster_id="cluster-demo",
            mode=KubernetesConfigMode.KUBECONFIG,
            context="offline-demo",
            api_server="https://offline.invalid",
        ),
        namespace="demo",
        name="fraud-api",
        uid="uid-fraud-api-demo",
        resource_version="100",
        generation=4,
        observed_generation=4,
        replicas=2,
        ready_replicas=2,
        updated_replicas=2,
        available_replicas=2,
        selector={"matchLabels": {"app": "fraud-api"}},
        pod_template={"spec": {"containers": [raw]}},
        containers=(container,),
        strategy_type="RollingUpdate",
        strategy={"type": "RollingUpdate"},
    )


def _operation(now):
    target = KubernetesTarget(
        cluster_id="cluster-demo",
        namespace="demo",
        name="fraud-api",
        container="fraud-api",
        uid="uid-fraud-api-demo",
        expected_resource_version="100",
    )
    typed = DeployImageDigest(
        operation_type=OperationType.DEPLOY_IMAGE_DIGEST,
        image="registry.example/fraud-api@sha256:" + ("a" * 64),
    )
    expiry = now + timedelta(hours=1)
    rollback = RollbackScope(
        authorized=True,
        target_uid=target.uid,
        restore_fields=(RollbackField.IMAGE,),
        expires_at=expiry,
    )
    operation_id = "OP-OFFLINE-DEMO"
    approval = ApprovalReceipt(
        approval_id="APR-OFFLINE-DEMO",
        issuer="offline-demo",
        approver_subject="offline-demo-operator",
        operation_id=operation_id,
        scope_hash=authorization_scope_hash(
            operation_id=operation_id,
            target=target,
            operation=typed,
            rollback_scope=rollback,
        ),
        approved_at=now,
        expires_at=expiry,
        rollback_scope=rollback,
    )
    return AuthorizedOperation(
        operation_id=operation_id,
        idempotency_key="IDEMP-" + ("0" * 24),
        correlation_id="CORR-OFFLINE-DEMO",
        trace_id="TRACE-OFFLINE-DEMO",
        issuer="offline-demo",
        issued_at=now,
        expires_at=expiry,
        environment="demo",
        target=target,
        operation=typed,
        approval=approval,
        authorization_token="AUTH-" + ("0" * 24),
    )


def _policy():
    return PreflightPolicy(
        allowed_targets=(
            AllowedDeploymentTarget(
                cluster_id="cluster-demo",
                namespace="demo",
                workload="fraud-api",
                container="fraud-api",
            ),
        )
    )


def _coordinator(path, inspector, mutation, clock, observer, verifier):
    journal = SQLiteExecutionJournal(path)
    return (
        ExecutionCoordinator(
            journal=journal,
            inspector=inspector,
            policy=_policy(),
            authorization_verifier=DemoAuthorizationVerifier(clock),
            mutation_adapter=mutation,
            recovery_verifier=verifier,
            rollout_observer=observer,
            now=clock,
        ),
        journal,
    )


def _summary(name, result, journal, mutation, *, resumed=False):
    events = journal.audit_events(result.execution_id)
    return {
        "scenario": name,
        "final_state": result.state,
        "operator_intervention_required": result.operator_intervention_required,
        "resumed_after_interruption": resumed,
        "mutation_directions": [item.value for item in mutation.calls],
        "audit_chain_valid": journal.verify_audit_chain(result.execution_id),
        "state_timeline": [
            item["to_state"]
            for item in events
            if item["event_type"] in {"EXECUTION_CREATED", "STATE_TRANSITION"}
        ],
    }


def run_demo(output_dir):
    """Run all offline scenarios and return a JSON-serializable report."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
    def clock():
        return now

    operation = _operation(now)
    plan = create_plan(operation)

    inspector = DemoInspector(_snapshot())
    mutation = DemoMutationAdapter(inspector)
    coordinator, journal = _coordinator(
        root / "success.db",
        inspector,
        mutation,
        clock,
        DemoRolloutObserver((RolloutOutcome.SUCCEEDED,)),
        DemoRecoveryVerifier(clock, (VerificationVerdict.VERIFIED,)),
    )
    success = coordinator.execute(operation, plan, execution_id="EXEC-DEMO-SUCCESS")
    success_mutation = mutation

    inspector = DemoInspector(_snapshot())
    mutation = DemoMutationAdapter(inspector)
    coordinator, rollback_journal = _coordinator(
        root / "rollback.db",
        inspector,
        mutation,
        clock,
        DemoRolloutObserver((RolloutOutcome.FAILED, RolloutOutcome.SUCCEEDED)),
        DemoRecoveryVerifier(clock, (VerificationVerdict.VERIFIED,)),
    )
    rollback = coordinator.execute(operation, plan, execution_id="EXEC-DEMO-ROLLBACK")
    rollback_mutation = mutation

    inspector = DemoInspector(_snapshot())
    mutation = DemoMutationAdapter(inspector)
    coordinator, restart_journal = _coordinator(
        root / "restart.db",
        inspector,
        mutation,
        clock,
        CrashOnceObserver(),
        DemoRecoveryVerifier(clock, (VerificationVerdict.VERIFIED,)),
    )
    interrupted = False
    try:
        coordinator.execute(operation, plan, execution_id="EXEC-DEMO-RESTART")
    except RuntimeError as exc:
        if "simulated process interruption" not in str(exc):
            raise
        interrupted = True
    resumed, _ = _coordinator(
        root / "restart.db",
        inspector,
        mutation,
        clock,
        DemoRolloutObserver((RolloutOutcome.SUCCEEDED,)),
        DemoRecoveryVerifier(clock, (VerificationVerdict.VERIFIED,)),
    )
    restart = resumed.execute(operation, plan, execution_id="IGNORED-ON-RESUME")
    restart_mutation = mutation

    report = {
        "mode": "OFFLINE_SIMULATION_ONLY",
        "cluster_contacted": False,
        "production_authorization_used": False,
        "scenarios": [
            _summary("successful deployment", success, journal, success_mutation),
            _summary(
                "failed rollout and exact rollback",
                rollback,
                rollback_journal,
                rollback_mutation,
            ),
            _summary(
                "restart reconciliation",
                restart,
                restart_journal,
                restart_mutation,
                resumed=interrupted,
            ),
        ],
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for disposable SQLite journals (defaults to a temporary directory)",
    )
    args = parser.parse_args(argv)
    if args.output_dir is not None:
        report = run_demo(args.output_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="agent4-offline-demo-") as directory:
            report = run_demo(directory)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
