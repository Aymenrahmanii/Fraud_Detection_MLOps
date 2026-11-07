"""Agent 4 composition: the platform's trusted boundaries around Agent 4's coordinator.

Agent 4 ships verifiers but, by design, no issuer, approval store, Agent 1
transport or target resolver. This module supplies exactly those, and nothing
that bypasses Agent 4's own checks:

* `TargetResolver`: logical service + intent -> exact `KubernetesTarget` and
  typed operation (a rollback to v2.6 becomes `DEPLOY_IMAGE_DIGEST` to v2.6's
  digest, because Agent 4's `ROLLBACK_EXECUTION` only undoes its own runs);
* `HitlTokenVerifier`: Agent 4's `AuthorizationVerifier`, checking the
  HITL-signed permit carried as the operation's authorization token;
* the production coordinator additionally gets Agent 4's own
  `SignedAuthorizationPermitVerifier` + `HitlProvenanceVerifier`, so a live
  production mutation needs a human-interactive approval;
* `Agent1VerdictProvider`: Agent 4's `RecoveryVerdictProvider`, returning a
  verdict signed by Agent 1.

`prepare()` builds and dry-runs the exact plan without mutating; `execute()`
rebuilds the same operation with the real approval receipt and runs the
coordinator, refusing if the plan hash changed in between.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import JsonValue

from agent4_deployment_recovery.adapters.signed_contracts import (
    Ed25519EnvelopeVerifier,
    SignedAgent1RecoveryVerifier,
    SignedAuthorizationPermitVerifier,
    TrustedIssuerRegistry,
)
from agent4_deployment_recovery.core.authorization import AuthorizationDecision
from agent4_deployment_recovery.core.coordinator import ExecutionCoordinator
from agent4_deployment_recovery.core.dry_run import server_side_dry_run
from agent4_deployment_recovery.core.execution_models import RolloutPolicy
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    RecoveryVerificationRequestV1,
    SignedRecoveryVerificationEnvelopeV1,
)
from agent4_deployment_recovery.core.kubernetes_models import PreflightDecision, PreflightPolicy
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import build_forward_artifact
from agent4_deployment_recovery.core.rollout import RolloutObserver
from agent4_deployment_recovery.core.schemas import (
    AuthorizedOperation,
    DeployImageDigest,
    KubernetesTarget,
    OperationType,
    RollbackField,
    RollbackScope,
    ScaleReplicas,
    SetContainerResources,
    authorization_scope_hash,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal
from aiops_platform.contracts.platform_payloads import PlanPrepared
from aiops_platform.hitl.provenance import HitlProvenanceVerifier
from aiops_platform.hitl.service import PROVIDER_ID, decode_token

ORCHESTRATOR_ISSUER = "aiops-orchestrator"
OPERATION_TTL = timedelta(minutes=30)
PRODUCTION = "production"


class UnresolvableAction(ValueError):
    """The intent cannot be made exact; Agent 4 must not guess (fail closed)."""


class TargetResolver(Protocol):
    def target(self, service_id: str, namespace: str) -> KubernetesTarget: ...

    def typed_operation(self, action: str, parameters: dict[str, JsonValue], target: KubernetesTarget):
        """Exact Agent 4 TypedOperation for an action intent, or raise UnresolvableAction."""

    def current_state(self, target: KubernetesTarget) -> dict[str, JsonValue]: ...


class ClusterTargetResolver:
    """Resolver over the simulated cluster (a real one would read the Deployment's history)."""

    def __init__(self, cluster) -> None:
        self._cluster = cluster

    def target(self, service_id: str, namespace: str) -> KubernetesTarget:
        return self._cluster.target(service_id, namespace)

    def typed_operation(self, action: str, parameters: dict[str, JsonValue], target: KubernetesTarget):
        dep = self._cluster.deployment(target.name, target.namespace)
        if action in ("ROLLBACK", "DEPLOY"):
            version = parameters.get("to_version") or parameters.get("version")
            if version is None and action == "ROLLBACK":
                previous = self._cluster.previous_release(target.name, target.namespace)
                if previous is None:
                    raise UnresolvableAction("no previous release to roll back to")
                version = previous.version
            release = dep.releases.get(str(version)) if version else None
            if release is None:
                raise UnresolvableAction(f"unknown release {version!r} for {target.name}")
            if release.image == dep.image:
                raise UnresolvableAction(f"{target.name} already runs {release.version}")
            return DeployImageDigest(operation_type=OperationType.DEPLOY_IMAGE_DIGEST, image=release.image)
        if action == "SCALE_REPLICAS":
            to = parameters.get("replicas", parameters.get("to"))
            if not isinstance(to, int):
                raise UnresolvableAction("scale intent has no exact replica count")
            if to == dep.replicas:
                raise UnresolvableAction(f"{target.name} already has {to} replicas")
            return ScaleReplicas(
                operation_type=OperationType.SCALE_REPLICAS, from_replicas=dep.replicas, to_replicas=to
            )
        if action == "INCREASE_MEMORY":
            limit = parameters.get("memory_limit_to_mib", parameters.get("memory_limit_mib"))
            if not isinstance(limit, int):
                raise UnresolvableAction("memory intent has no exact limit")
            return SetContainerResources(operation_type=OperationType.SET_CONTAINER_RESOURCES, memory_limit_mib=limit)
        raise UnresolvableAction(f"Agent 4 cannot execute {action}")

    def current_state(self, target: KubernetesTarget) -> dict[str, JsonValue]:
        dep = self._cluster.deployment(target.name, target.namespace)
        return {
            "version": dep.version,
            "image": dep.image,
            "replicas": dep.replicas,
            "memory_limit_mib": dep.memory_limit_mib,
            "resource_version": str(dep.resource_version),
        }


ROLLBACK_FIELD = {
    DeployImageDigest: RollbackField.IMAGE,
    ScaleReplicas: RollbackField.REPLICAS,
    SetContainerResources: RollbackField.RESOURCES,
}


class HitlTokenVerifier:
    """Agent 4 `AuthorizationVerifier`: the operation's token is a HITL-signed permit."""

    verifier_id = "aiops-hitl-token-v1"

    def __init__(self, registry: TrustedIssuerRegistry) -> None:
        self._signatures = Ed25519EnvelopeVerifier(registry)

    def verify(self, operation: AuthorizedOperation) -> AuthorizationDecision:
        now = datetime.now(UTC)
        reason = self._check(operation, now)
        return AuthorizationDecision(
            authorized=reason is None,
            verifier_id=self.verifier_id,
            checked_at=now,
            reason=reason or "HITL permit verified for this exact operation",
            authorization_reference=operation.approval.approval_id if reason is None else None,
        )

    def _check(self, operation: AuthorizedOperation, now: datetime) -> str | None:
        try:
            envelope = decode_token(operation.authorization_token.get_secret_value())
            self._signatures.verify(
                key_id=envelope.key_id,
                issuer=envelope.claims.issuer,
                audience=envelope.claims.audience,
                payload=envelope.claims,
                signature=envelope.signature,
            )
        except Exception as exc:  # noqa: BLE001 - any decoding/signature problem denies
            return f"authorization token rejected: {exc}"
        claims = envelope.claims
        if (claims.operation_id, claims.correlation_id, claims.environment) != (
            operation.operation_id,
            operation.correlation_id,
            operation.environment,
        ):
            return "permit was issued for a different operation"
        if claims.target != operation.target or claims.operation != operation.operation:
            return "permit was issued for a different target or change"
        if claims.approval.receipt_id != operation.approval.approval_id:
            return "permit does not reference this approval"
        if now >= claims.expires_at:
            return "permit is expired"
        if operation.environment == PRODUCTION and claims.approval.method != ApprovalMethod.HUMAN_INTERACTIVE:
            return "production mutation requires a human approval"
        return None


class Agent1VerdictProvider:
    """Agent 4 `RecoveryVerdictProvider`; `verify` asks Agent 1 for a signed verdict."""

    def __init__(
        self, verify: Callable[[RecoveryVerificationRequestV1, dict[str, str]], SignedRecoveryVerificationEnvelopeV1]
    ):
        self._verify = verify
        self.service: dict[str, str] = {}

    def get_verdict(self, request: RecoveryVerificationRequestV1) -> SignedRecoveryVerificationEnvelopeV1:
        return self._verify(request, dict(self.service))


@dataclass
class ExecutionOutcome:
    status: str  # SUCCESS / FAILED / BLOCKED
    state: str
    reason: str
    execution_id: str
    pre_state: dict[str, JsonValue]
    post_state: dict[str, JsonValue]
    operator_intervention_required: bool


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class Agent4Executor:
    def __init__(
        self,
        *,
        journal_path: str | Path,
        inspector,
        mutation_adapter,
        resolver: TargetResolver,
        preflight_policy: Callable[[], PreflightPolicy],
        hitl_registry: TrustedIssuerRegistry,
        agent1_registry: TrustedIssuerRegistry,
        approval_lookup: Callable[[str], dict[str, Any] | None],
        verdicts: Agent1VerdictProvider,
        rollout_policy: RolloutPolicy | None = None,
    ) -> None:
        self._journal = SQLiteExecutionJournal(journal_path)
        self._inspector = inspector
        self._mutation = mutation_adapter
        self._resolver = resolver
        self._policy = preflight_policy
        self._hitl_registry = hitl_registry
        self._agent1_registry = agent1_registry
        self._lookup = approval_lookup
        self._verdicts = verdicts
        self._rollout = rollout_policy or RolloutPolicy()
        self._lock = threading.Lock()  # one mutation at a time per worker

    # ---- prepare (no side effect) -----------------------------------------

    def prepare(
        self,
        *,
        incident_id: str,
        trace_id: str,
        environment: str,
        service_id: str,
        namespace: str,
        action_id: str,
        action_version: int,
        action: str,
        parameters: dict[str, JsonValue],
    ) -> PlanPrepared:
        def rejected(reason: str) -> PlanPrepared:
            return PlanPrepared(action_id=action_id, action_version=action_version, status="REJECTED", reason=reason)

        try:
            target = self._resolver.target(service_id, namespace)
            typed = self._resolver.typed_operation(action, parameters, target)
        except (UnresolvableAction, Exception) as exc:  # noqa: BLE001 - fail closed
            return rejected(str(exc))
        now = datetime.now(UTC).replace(microsecond=0)
        expires = now + OPERATION_TTL
        rollback = RollbackScope(
            authorized=True,
            target_uid=target.uid,
            restore_fields=(ROLLBACK_FIELD[type(typed)],),
            expires_at=expires,
        )
        operation_id = f"OP-{action_id}-v{action_version}"
        blob = {
            "operation_id": operation_id,
            "idempotency_key": f"IDEMP-{incident_id}-{action_id}-v{action_version}"[:128],
            "correlation_id": incident_id,
            "trace_id": trace_id[:128],
            "environment": environment,
            "issued_at": _iso(now),
            "expires_at": _iso(expires),
            "target": target.model_dump(mode="json"),
            "operation": typed.model_dump(mode="json"),
            "rollback_scope": rollback.model_dump(mode="json"),
            "service_id": service_id,
            "namespace": namespace,
        }
        provisional = self._operation(
            blob,
            receipt={
                "approval_id": f"PENDING-{operation_id}"[:128],
                "issuer": PROVIDER_ID,
                "approver_subject": "pending-approval",
                "approved_at": _iso(now),
            },
            token="pending-" + "0" * 24,
        )
        plan = create_plan(provisional)
        snapshot = self._inspector.inspect_deployment(provisional.target)
        preflight = run_preflight(provisional, snapshot, self._policy(), requested_strategy=plan.strategy)
        if preflight.decision != PreflightDecision.PASS:
            return rejected("preflight: " + "; ".join(r.code.value for r in preflight.reasons))
        preview = build_typed_patch(provisional, snapshot, preflight)
        dry_run = server_side_dry_run(self._inspector, provisional, preview, contact_cluster=True)
        if not dry_run.admission_accepted:
            return rejected(f"dry-run: {dry_run.reason}")
        artifact = build_forward_artifact(plan, snapshot, preflight, preview, dry_run)
        return PlanPrepared(
            action_id=action_id,
            action_version=action_version,
            status="PREPARED",
            reason="exact plan prepared and accepted by server-side dry-run",
            operation_id=operation_id,
            plan_hash=plan.plan_hash,
            patch_hash=artifact.patch_bytes_hash,
            authorization_scope_hash=plan.authorization_scope_hash,
            target=blob["target"],
            patch=tuple(op.model_dump(mode="json") for op in artifact.patch),
            operation=blob,
        )

    # ---- execute -----------------------------------------------------------

    def execute(
        self, prepared: PlanPrepared, *, receipt: dict[str, JsonValue], approval_token: str
    ) -> ExecutionOutcome:
        blob = prepared.operation
        operation = self._operation(blob, receipt=receipt, token=approval_token)
        plan = create_plan(operation)
        execution_id = f"EXEC-{operation.operation_id}"[:128]
        before = self._resolver.current_state(operation.target)
        if plan.plan_hash != prepared.plan_hash:
            return ExecutionOutcome(
                "BLOCKED",
                "REJECTED",
                "plan changed since approval; re-approval required",
                execution_id,
                before,
                before,
                True,
            )
        production = operation.environment == PRODUCTION
        coordinator = self._coordinator(production=production)
        signed = decode_token(approval_token) if production else None
        with self._lock:
            self._verdicts.service = {
                "service_id": str(blob["service_id"]),
                "namespace": str(blob["namespace"]),
                "environment": operation.environment,
            }
            result = coordinator.execute(operation, plan, execution_id=execution_id, signed_authorization=signed)
        after = self._resolver.current_state(operation.target)
        status = {"SUCCEEDED": "SUCCESS", "REJECTED": "BLOCKED"}.get(result.state, "FAILED")
        return ExecutionOutcome(
            status,
            result.state,
            result.reason,
            result.execution_id,
            before,
            after,
            result.operator_intervention_required,
        )

    def audit_chain_valid(self, execution_id: str) -> bool:
        return self._journal.verify_audit_chain(execution_id)

    # ---- internals ---------------------------------------------------------

    def _coordinator(self, *, production: bool) -> ExecutionCoordinator:
        external = (
            SignedAuthorizationPermitVerifier(
                self._hitl_registry,
                approval_verifier=HitlProvenanceVerifier(self._lookup),
                replay_store=self._journal,
            )
            if production
            else None
        )
        return ExecutionCoordinator(
            journal=self._journal,
            inspector=self._inspector,
            policy=self._policy(),
            authorization_verifier=HitlTokenVerifier(self._hitl_registry),
            external_authorization_verifier=external,
            allowed_live_environments=frozenset({PRODUCTION}) if production else frozenset(),
            mutation_adapter=self._mutation,
            recovery_verifier=SignedAgent1RecoveryVerifier(
                self._agent1_registry, replay_store=self._journal, provider=self._verdicts
            ),
            rollout_observer=RolloutObserver(self._rollout),
        )

    @staticmethod
    def _operation(blob: dict[str, Any], *, receipt: dict[str, Any], token: str) -> AuthorizedOperation:
        rollback = blob["rollback_scope"]
        scope = authorization_scope_hash(
            operation_id=blob["operation_id"],
            target=KubernetesTarget.model_validate_json(json.dumps(blob["target"])),
            operation=_typed(blob["operation"]),
            rollback_scope=RollbackScope.model_validate_json(json.dumps(rollback)),
        )
        payload = {
            "operation_id": blob["operation_id"],
            "idempotency_key": blob["idempotency_key"],
            "correlation_id": blob["correlation_id"],
            "trace_id": blob["trace_id"],
            "issuer": ORCHESTRATOR_ISSUER,
            "issued_at": blob["issued_at"],
            "expires_at": blob["expires_at"],
            "environment": blob["environment"],
            "target": blob["target"],
            "operation": blob["operation"],
            "approval": {
                "approval_id": receipt["approval_id"],
                "issuer": receipt["issuer"],
                "approver_subject": receipt["approver_subject"],
                "operation_id": blob["operation_id"],
                "scope_hash": scope,
                "approved_at": receipt["approved_at"],
                "expires_at": blob["expires_at"],
                "rollback_scope": rollback,
            },
            "authorization_token": token,
        }
        # Agent 4 models are strict (no coercion), so validate from JSON.
        return AuthorizedOperation.model_validate_json(json.dumps(payload))


def _typed(operation: dict[str, Any]):
    kind = {
        "DEPLOY_IMAGE_DIGEST": DeployImageDigest,
        "SCALE_REPLICAS": ScaleReplicas,
        "SET_CONTAINER_RESOURCES": SetContainerResources,
    }[operation["operation_type"]]
    return kind.model_validate_json(json.dumps(operation))
