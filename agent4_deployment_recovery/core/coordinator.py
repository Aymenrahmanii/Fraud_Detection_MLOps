"""Controlled Phase 3 execution coordinator.

There is no public CLI entry point for this service. Construction defaults to
deny-all authorization, disabled mutation, and unavailable Agent 1 evidence.
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from agent4_deployment_recovery.adapters.agent1_verification import (
    RecoveryVerifier,
    UnavailableRecoveryVerifier,
)
from agent4_deployment_recovery.adapters.kubernetes import KubernetesInspectionAdapter
from agent4_deployment_recovery.adapters.kubernetes_mutation import (
    DisabledKubernetesMutationAdapter,
    KubernetesMutationAdapter,
)
from agent4_deployment_recovery.adapters.signed_contracts import (
    SignedAuthorizationPermitVerifier,
)
from agent4_deployment_recovery.core.authorization import (
    AuthorizationDecision,
    AuthorizationVerifier,
    DenyAllAuthorizationVerifier,
    require_authorized,
)
from agent4_deployment_recovery.core.canonical import canonical_json, canonical_sha256
from agent4_deployment_recovery.core.dry_run import server_side_dry_run
from agent4_deployment_recovery.core.errors import (
    AuthorizationDeniedError,
    KubernetesAdapterError,
    KubernetesConnectivityError,
    KubernetesTimeoutError,
    MutationDisabledError,
    SignedContractError,
    UnsafeRollbackError,
)
from agent4_deployment_recovery.core.execution_models import (
    CoordinatorResult,
    LiveMutationPermit,
    MutationOutcome,
    MutationResult,
    ReconciliationOutcome,
    RolloutObservationResult,
    RolloutOutcome,
    ValidatedPatchArtifact,
)
from agent4_deployment_recovery.core.integration_models import (
    AuthorizationBinding,
    RecoveryVerificationRequestV1,
    SignedAuthorizationEnvelopeV1,
    VerifiedExternalAuthorization,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    PreflightDecision,
    PreflightPolicy,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import (
    build_exact_rollback_artifact,
    build_forward_artifact,
    matches_exact_prior_state,
    reconcile_uncertain_mutation,
)
from agent4_deployment_recovery.core.rollout import RolloutObserver
from agent4_deployment_recovery.core.schemas import (
    AuthorizedOperation,
    DeployImageDigest,
    DeploymentPlan,
    ExecutionRecord,
    ExecutionState,
    NoOp,
    RecoveryVerification,
    VerificationVerdict,
)
from agent4_deployment_recovery.core.state_machine import is_terminal
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal

FORWARD_ARTIFACT = "phase3.forward_patch"
PREFLIGHT_ARTIFACT = "phase3.preflight"
DRY_RUN_ARTIFACT = "phase3.dry_run"
ROLLOUT_ARTIFACT = "phase3.rollout"
VERIFICATION_ARTIFACT = "phase3.agent1_verification"
ROLLBACK_ARTIFACT = "phase3.rollback_patch"
MUTATION_RESULT_ARTIFACT = "phase3.mutation_result"
ROLLBACK_RESULT_ARTIFACT = "phase3.rollback_result"
ROLLBACK_ROLLOUT_ARTIFACT = "phase3.rollback_rollout"
ROLLBACK_VERIFICATION_ARTIFACT = "phase3.rollback_agent1_verification"
EXTERNAL_AUTHORIZATION_ARTIFACT = "phase4.external_authorization"
VERIFICATION_REQUEST_ARTIFACT = "phase4.agent1_verification_request"
ROLLBACK_VERIFICATION_REQUEST_ARTIFACT = "phase4.rollback_agent1_verification_request"


class ExecutionCoordinator:
    """Drive one hash-bound execution while preserving durable state gates."""

    def __init__(
        self,
        *,
        journal: SQLiteExecutionJournal,
        inspector: KubernetesInspectionAdapter,
        policy: PreflightPolicy,
        authorization_verifier: AuthorizationVerifier | None = None,
        external_authorization_verifier: SignedAuthorizationPermitVerifier
        | None = None,
        authorization_audience: str = "agent4-deployment-recovery",
        allowed_live_environments: frozenset[str] = frozenset(),
        agent1_audience: str = "agent4-deployment-recovery",
        mutation_adapter: KubernetesMutationAdapter | None = None,
        recovery_verifier: RecoveryVerifier | None = None,
        rollout_observer: RolloutObserver | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._journal = journal
        self._inspector = inspector
        self._policy = policy
        self._authorization = authorization_verifier or DenyAllAuthorizationVerifier()
        self._external_authorization = external_authorization_verifier
        self._authorization_audience = authorization_audience
        self._allowed_live_environments = allowed_live_environments
        self._agent1_audience = agent1_audience
        if (
            external_authorization_verifier is not None
            and not allowed_live_environments
        ):
            raise ValueError(
                "signed live authorization requires an explicit environment allowlist"
            )
        self._mutation = mutation_adapter or DisabledKubernetesMutationAdapter()
        self._recovery = recovery_verifier or UnavailableRecoveryVerifier()
        self._observer = rollout_observer or RolloutObserver()
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _reason(value: str) -> str:
        return value[:512] or "unspecified execution failure"

    @staticmethod
    def _validate_binding(operation: AuthorizedOperation, plan: DeploymentPlan) -> None:
        expected = (
            operation.operation_id,
            operation.idempotency_key,
            operation.correlation_id,
            operation.trace_id,
            operation.environment,
            operation.issued_at,
            operation.expires_at,
            operation.target,
            operation.operation,
            operation.approval.scope_hash,
            operation.approval.rollback_scope,
        )
        actual = (
            plan.operation_id,
            plan.idempotency_key,
            plan.correlation_id,
            plan.trace_id,
            plan.environment,
            plan.created_at,
            plan.expires_at,
            plan.target,
            plan.operation,
            plan.authorization_scope_hash,
            plan.rollback_scope,
        )
        if actual != expected:
            raise AuthorizationDeniedError(
                "deployment plan is not bound to the exact authorized operation"
            )

    def _persist_model(self, execution_id: str, artifact_type: str, model) -> None:
        content = canonical_json(model)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._journal.record_artifact(
            execution_id,
            artifact_type,
            content,
            digest,
            now=self._now(),
        )

    def _load_patch(
        self, execution_id: str, artifact_type: str
    ) -> ValidatedPatchArtifact:
        content, _ = self._journal.get_artifact(execution_id, artifact_type)
        return ValidatedPatchArtifact.model_validate_json(content)

    def _verification_request(
        self,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        rollback: bool,
    ) -> RecoveryVerificationRequestV1:
        artifact_type = (
            ROLLBACK_VERIFICATION_REQUEST_ARTIFACT
            if rollback
            else VERIFICATION_REQUEST_ARTIFACT
        )
        try:
            content, _ = self._journal.get_artifact(record.execution_id, artifact_type)
            return RecoveryVerificationRequestV1.model_validate_json(content)
        except KeyError:
            expected_image = None
            if isinstance(plan.operation, DeployImageDigest):
                expected_image = (
                    artifact.rollback_snapshot.prior_image
                    if rollback
                    else plan.operation.image
                )
            expected_version = None
            rollout_artifact = (
                ROLLBACK_ROLLOUT_ARTIFACT if rollback else ROLLOUT_ARTIFACT
            )
            try:
                rollout_content, _ = self._journal.get_artifact(
                    record.execution_id, rollout_artifact
                )
                rollout = RolloutObservationResult.model_validate_json(rollout_content)
                if rollout.last_snapshot is not None:
                    expected_version = rollout.last_snapshot.resource_version
            except KeyError:
                pass
            if expected_image is None and expected_version is None:
                raise SignedContractError(
                    "Agent 1 request lacks an observed expected image or workload version"
                )
            requested_at = self._now()
            seed = canonical_sha256(
                {
                    "execution_id": record.execution_id,
                    "phase": "ROLLBACK" if rollback else "FORWARD",
                    "patch_hash": artifact.patch_bytes_hash,
                    "requested_at": requested_at,
                }
            )
            request = RecoveryVerificationRequestV1(
                request_id=f"VERIFY-REQ-{seed[:20]}",
                audience=self._agent1_audience,
                execution_id=record.execution_id,
                correlation_id=record.correlation_id,
                target_uid=record.target.uid,
                expected_plan_hash=plan.plan_hash,
                expected_patch_hash=artifact.patch_bytes_hash,
                expected_image=expected_image,
                expected_version=expected_version,
                requested_at=requested_at,
            )
            self._persist_model(record.execution_id, artifact_type, request)
            return request

    def _request_recovery_verification(
        self,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        rollback: bool,
    ) -> RecoveryVerification:
        bound = getattr(self._recovery, "verify_bound", None)
        if bound is not None:
            request = self._verification_request(plan, record, artifact, rollback=rollback)
            return bound(request)
        return self._recovery.verify(plan, record)

    def _transition(
        self,
        record: ExecutionRecord,
        state: ExecutionState,
        **kwargs,
    ) -> ExecutionRecord:
        return self._journal.transition(
            record.execution_id,
            state,
            expected_version=record.version,
            now=self._now(),
            **kwargs,
        )

    def _result(
        self,
        record: ExecutionRecord,
        reason: str,
        *,
        operator: bool = False,
    ) -> CoordinatorResult:
        return CoordinatorResult(
            execution_id=record.execution_id,
            state=record.state.value,
            reason=self._reason(reason),
            operator_intervention_required=operator,
        )

    def _permit(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        artifact: ValidatedPatchArtifact,
        record: ExecutionRecord,
        *,
        signed_authorization: SignedAuthorizationEnvelopeV1 | None = None,
        reuse_external_authorization: bool = False,
    ) -> tuple[AuthorizationDecision, LiveMutationPermit]:
        current = self._now()
        if plan.expires_at <= current:
            raise AuthorizationDeniedError("deployment plan is expired")
        decision = require_authorized(operation, self._authorization, at=current)
        if artifact.plan_hash != plan.plan_hash:
            raise AuthorizationDeniedError("persisted patch has a different plan hash")
        verified = None
        if self._external_authorization is not None:
            if operation.environment not in self._allowed_live_environments:
                raise AuthorizationDeniedError(
                    "environment is outside the signed execution allowlist"
                )
            if reuse_external_authorization:
                try:
                    content, _ = self._journal.get_artifact(
                        record.execution_id, EXTERNAL_AUTHORIZATION_ARTIFACT
                    )
                    verified = VerifiedExternalAuthorization.model_validate_json(
                        content
                    )
                except KeyError as exc:
                    raise AuthorizationDeniedError(
                        "persisted external authorization is unavailable for rollback"
                    ) from exc
                if current >= verified.expires_at:
                    raise AuthorizationDeniedError(
                        "persisted external authorization is expired for rollback"
                    )
            else:
                if signed_authorization is None:
                    raise AuthorizationDeniedError(
                        "signed external authorization is required for live mutation"
                    )
                try:
                    verified = self._external_authorization.verify_and_consume(
                        signed_authorization,
                        AuthorizationBinding(
                            audience=self._authorization_audience,
                            operation_id=operation.operation_id,
                            correlation_id=operation.correlation_id,
                            environment=operation.environment,
                            target=operation.target,
                            operation=operation.operation,
                            plan_hash=plan.plan_hash,
                            patch_hash=artifact.patch_bytes_hash,
                            authorization_scope_hash=plan.authorization_scope_hash,
                            rollback_scope=plan.rollback_scope,
                        ),
                        execution_id=record.execution_id,
                        at=current,
                    )
                except SignedContractError as exc:
                    raise AuthorizationDeniedError(str(exc)) from exc
        return decision, LiveMutationPermit(
            operation_id=operation.operation_id,
            plan_hash=plan.plan_hash,
            authorization_scope_hash=plan.authorization_scope_hash,
            rollback_scope_hash=artifact.rollback_scope_hash,
            patch_bytes_hash=artifact.patch_bytes_hash,
            direction=artifact.direction,
            verifier_id=(
                self._external_authorization.verifier_id
                if verified is not None and self._external_authorization is not None
                else decision.verifier_id
            ),
            authorization_reference=(
                verified.approval_reference
                if verified is not None
                else decision.authorization_reference or "missing-reference"
            ),
            environment=operation.environment,
            external_permit_id=verified.permit_id if verified is not None else None,
            external_claims_hash=verified.claims_hash if verified is not None else None,
            approval_provenance_reference=(
                verified.approval_reference if verified is not None else None
            ),
            checked_at=decision.checked_at,
        )

    def execute(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        *,
        execution_id: str,
        signed_authorization: SignedAuthorizationEnvelopeV1 | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CoordinatorResult:
        """Execute through trusted injected boundaries; never exposed by the CLI."""
        self._validate_binding(operation, plan)
        record, created = self._journal.create_execution(
            plan,
            execution_id=execution_id,
            approval_id=operation.approval.approval_id,
            now=self._now(),
        )
        if not created:
            return self._resume(
                operation,
                plan,
                record,
                signed_authorization=signed_authorization,
                cancelled=cancelled,
            )
        try:
            decision = require_authorized(
                operation, self._authorization, at=self._now()
            )
        except AuthorizationDeniedError as exc:
            record = self._transition(
                record, ExecutionState.REJECTED, reason=self._reason(str(exc))
            )
            return self._result(record, str(exc))
        record = self._transition(
            record,
            ExecutionState.AUTHORIZED,
            authorization_verifier_id=decision.verifier_id,
        )
        try:
            snapshot = self._inspector.inspect_deployment(operation.target)
            preflight = run_preflight(
                operation,
                snapshot,
                self._policy,
                requested_strategy=plan.strategy,
                at=self._now(),
            )
        except KubernetesAdapterError as exc:
            record = self._transition(
                record, ExecutionState.FAILED, reason=self._reason(str(exc))
            )
            return self._result(record, str(exc))
        self._persist_model(record.execution_id, PREFLIGHT_ARTIFACT, preflight)
        if preflight.decision != PreflightDecision.PASS:
            reason = "; ".join(item.code.value for item in preflight.reasons)
            record = self._transition(
                record, ExecutionState.REJECTED, reason=self._reason(reason)
            )
            return self._result(record, reason)
        record = self._transition(record, ExecutionState.PREFLIGHT_PASSED)
        record = self._transition(record, ExecutionState.PLANNED)
        if isinstance(operation.operation, NoOp):
            record = self._transition(record, ExecutionState.SUCCEEDED)
            return self._result(
                record, "authorized no-op recorded without infrastructure mutation"
            )
        preview = build_typed_patch(operation, snapshot, preflight)
        dry_run = server_side_dry_run(
            self._inspector,
            operation,
            preview,
            contact_cluster=True,
        )
        self._persist_model(record.execution_id, DRY_RUN_ARTIFACT, dry_run)
        if not dry_run.admission_accepted:
            record = self._transition(
                record, ExecutionState.FAILED, reason=self._reason(dry_run.reason)
            )
            return self._result(record, dry_run.reason)
        artifact = build_forward_artifact(plan, snapshot, preflight, preview, dry_run)
        self._persist_model(record.execution_id, FORWARD_ARTIFACT, artifact)
        record = self._transition(record, ExecutionState.DRY_RUN_PASSED)
        return self._apply_forward(
            operation,
            plan,
            record,
            artifact,
            snapshot_generation=snapshot.generation,
            signed_authorization=signed_authorization,
            cancelled=cancelled,
        )

    def _apply_forward(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        snapshot_generation: int,
        signed_authorization: SignedAuthorizationEnvelopeV1 | None,
        cancelled: Callable[[], bool] | None,
        reuse_external_authorization: bool = False,
    ) -> CoordinatorResult:
        try:
            _, permit = self._permit(
                operation,
                plan,
                artifact,
                record,
                signed_authorization=signed_authorization,
                reuse_external_authorization=reuse_external_authorization,
            )
            current = self._inspector.inspect_deployment(operation.target)
            reconciliation = reconcile_uncertain_mutation(artifact, current)
            if current.resource_version != artifact.base_resource_version:
                raise AuthorizationDeniedError(
                    "resourceVersion changed after dry-run; refusing stale patch"
                )
            if reconciliation.outcome != ReconciliationOutcome.NOT_APPLIED:
                raise AuthorizationDeniedError(
                    "current affected fields no longer match captured prior values"
                )
        except (AuthorizationDeniedError, KubernetesAdapterError) as exc:
            record = self._transition(
                record, ExecutionState.FAILED, reason=self._reason(str(exc))
            )
            return self._result(record, str(exc))
        record = self._transition(record, ExecutionState.EXECUTING)
        try:
            mutation = self._mutation.apply_validated_patch(
                operation.target, artifact, permit
            )
        except (KubernetesTimeoutError, KubernetesConnectivityError) as exc:
            mutation = MutationResult(
                outcome=MutationOutcome.UNCERTAIN,
                reason=str(exc),
            )
            uncertainty = mutation.reason
        except (KubernetesAdapterError, MutationDisabledError) as exc:
            record = self._transition(
                record, ExecutionState.FAILED, reason=self._reason(str(exc))
            )
            return self._result(record, str(exc))
        else:
            uncertainty = mutation.reason
        self._persist_model(record.execution_id, MUTATION_RESULT_ARTIFACT, mutation)
        if mutation.outcome == MutationOutcome.UNCERTAIN:
            record = self._transition(record, ExecutionState.OUTCOME_UNCERTAIN)
            return self._reconcile_forward(
                operation,
                plan,
                record,
                artifact,
                expected_generation=(
                    mutation.response_generation
                    if mutation.response_generation is not None
                    else snapshot_generation + 1
                ),
                uncertainty=uncertainty,
                cancelled=cancelled,
            )
        expected_generation = mutation.response_generation or snapshot_generation + 1
        record = self._transition(record, ExecutionState.OBSERVING)
        return self._observe_and_verify(
            operation,
            plan,
            record,
            artifact,
            expected_generation=expected_generation,
            cancelled=cancelled,
        )

    def _reconcile_forward(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        expected_generation: int,
        uncertainty: str,
        cancelled: Callable[[], bool] | None,
    ) -> CoordinatorResult:
        try:
            snapshot = self._inspector.inspect_deployment(operation.target)
        except KubernetesAdapterError as exc:
            return self._result(
                record,
                f"mutation outcome remains uncertain: {uncertainty}; reconciliation failed: {exc}",
                operator=True,
            )
        reconciliation = reconcile_uncertain_mutation(artifact, snapshot)
        if reconciliation.outcome == ReconciliationOutcome.APPLIED:
            record = self._transition(record, ExecutionState.OBSERVING)
            return self._observe_and_verify(
                operation,
                plan,
                record,
                artifact,
                expected_generation=max(expected_generation, snapshot.generation),
                cancelled=cancelled,
            )
        if reconciliation.outcome == ReconciliationOutcome.NOT_APPLIED:
            reason = f"mutation was not applied after uncertain response: {uncertainty}"
            record = self._transition(
                record, ExecutionState.FAILED, reason=self._reason(reason)
            )
            return self._result(record, reason)
        return self._result(
            record,
            f"mutation outcome is divergent and cannot be reconciled safely: {uncertainty}",
            operator=True,
        )

    def _observe_and_verify(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        expected_generation: int,
        cancelled: Callable[[], bool] | None,
    ) -> CoordinatorResult:
        try:
            content, _ = self._journal.get_artifact(
                record.execution_id, ROLLOUT_ARTIFACT
            )
            rollout = RolloutObservationResult.model_validate_json(content)
        except KeyError:
            rollout = self._observer.observe(
                self._inspector,
                operation.target,
                expected_generation=expected_generation,
                cancelled=cancelled,
            )
            self._persist_model(record.execution_id, ROLLOUT_ARTIFACT, rollout)
        if rollout.outcome != RolloutOutcome.SUCCEEDED:
            reason = f"rollout {rollout.outcome.value.lower()}: {rollout.reason}"
            record = self._transition(
                record,
                ExecutionState.ROLLBACK_PENDING,
                rollback_execution_id=f"{record.execution_id}-RB",
            )
            return self._rollback(operation, plan, record, artifact, reason=reason)
        record = self._transition(record, ExecutionState.AWAITING_AGENT1)
        return self._verify_agent1(operation, plan, record, artifact)

    def _verify_agent1(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
    ) -> CoordinatorResult:
        try:
            content, _ = self._journal.get_artifact(
                record.execution_id, VERIFICATION_ARTIFACT
            )
            verification = RecoveryVerification.model_validate_json(content)
        except KeyError:
            try:
                verification = self._request_recovery_verification(
                    plan, record, artifact, rollback=False
                )
            except SignedContractError as exc:
                record = self._transition(
                    record,
                    ExecutionState.ROLLBACK_PENDING,
                    rollback_execution_id=f"{record.execution_id}-RB",
                )
                return self._rollback(
                    operation,
                    plan,
                    record,
                    artifact,
                    reason=f"Agent 1 evidence rejected: {self._reason(str(exc))}",
                )
            self._persist_model(
                record.execution_id, VERIFICATION_ARTIFACT, verification
            )
        if verification.verdict == VerificationVerdict.VERIFIED:
            record = self._transition(
                record, ExecutionState.SUCCEEDED, verification=verification
            )
            return self._result(record, "Agent 1 independently verified recovery")
        if verification.verdict == VerificationVerdict.FAILED:
            record = self._transition(
                record,
                ExecutionState.ROLLBACK_PENDING,
                verification=verification,
                rollback_execution_id=f"{record.execution_id}-RB",
            )
            return self._rollback(
                operation,
                plan,
                record,
                artifact,
                reason="Agent 1 verification failed",
            )
        record = self._transition(
            record,
            ExecutionState.VERIFICATION_INCONCLUSIVE,
            verification=verification,
        )
        return self._result(
            record,
            f"Agent 1 verification is {verification.verdict.value.lower()}",
            operator=True,
        )

    def _rollback(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        forward: ValidatedPatchArtifact,
        *,
        reason: str,
    ) -> CoordinatorResult:
        scope = operation.approval.rollback_scope
        try:
            operation.ensure_fresh(at=self._now())
            if not scope.authorized or scope.target_uid != operation.target.uid:
                raise UnsafeRollbackError(
                    "exact rollback is not authorized for this target"
                )
            if scope.expires_at is None or scope.expires_at <= self._now():
                raise UnsafeRollbackError("rollback authorization is expired")
            current = self._inspector.inspect_deployment(operation.target)
            rollback = build_exact_rollback_artifact(plan, forward, current)
            self._persist_model(record.execution_id, ROLLBACK_ARTIFACT, rollback)
            _, permit = self._permit(
                operation,
                plan,
                rollback,
                record,
                reuse_external_authorization=True,
            )
        except (
            ValueError,
            AuthorizationDeniedError,
            KubernetesAdapterError,
            UnsafeRollbackError,
        ) as exc:
            record = self._transition(
                record,
                ExecutionState.ROLLBACK_FAILED,
                reason=self._reason(f"{reason}; rollback blocked: {exc}"),
            )
            return self._result(record, record.failure_reason or reason, operator=True)
        record = self._transition(record, ExecutionState.ROLLING_BACK)
        try:
            result = self._mutation.apply_validated_patch(
                operation.target, rollback, permit
            )
        except (KubernetesTimeoutError, KubernetesConnectivityError) as exc:
            result = MutationResult(
                outcome=MutationOutcome.UNCERTAIN,
                reason=str(exc),
            )
        except (KubernetesAdapterError, MutationDisabledError) as exc:
            record = self._transition(
                record,
                ExecutionState.ROLLBACK_FAILED,
                reason=self._reason(f"{reason}; rollback request failed: {exc}"),
            )
            return self._result(record, record.failure_reason or reason, operator=True)
        self._persist_model(record.execution_id, ROLLBACK_RESULT_ARTIFACT, result)
        try:
            restored = self._inspector.inspect_deployment(operation.target)
        except KubernetesAdapterError as exc:
            return self._result(
                record,
                f"rollback outcome cannot be reconciled: {exc}",
                operator=True,
            )
        if matches_exact_prior_state(forward, restored):
            expected_generation = (
                result.response_generation or rollback.base_generation + 1
            )
            try:
                content, _ = self._journal.get_artifact(
                    record.execution_id, ROLLBACK_ROLLOUT_ARTIFACT
                )
                rollout = RolloutObservationResult.model_validate_json(content)
            except KeyError:
                rollout = self._observer.observe(
                    self._inspector,
                    operation.target,
                    expected_generation=expected_generation,
                )
                self._persist_model(
                    record.execution_id, ROLLBACK_ROLLOUT_ARTIFACT, rollout
                )
            if rollout.outcome != RolloutOutcome.SUCCEEDED:
                rollout_reason = (
                    f"{reason}; rollback rollout {rollout.outcome.value.lower()}: "
                    f"{rollout.reason}"
                )
                record = self._transition(
                    record,
                    ExecutionState.ROLLBACK_FAILED,
                    reason=self._reason(rollout_reason),
                )
                return self._result(record, rollout_reason, operator=True)
            record = self._transition(record, ExecutionState.ROLLBACK_AWAITING_AGENT1)
            return self._verify_rollback(plan, record, rollback, reason=reason)
        if result.outcome == MutationOutcome.UNCERTAIN:
            return self._result(
                record,
                f"{reason}; rollback outcome remains uncertain and was not retried",
                operator=True,
            )
        rollback_reason = (
            f"{reason}; rollback outcome {result.outcome.value.lower()} did not restore "
            "the exact captured prior state"
        )
        record = self._transition(
            record,
            ExecutionState.ROLLBACK_FAILED,
            reason=self._reason(rollback_reason),
        )
        return self._result(record, rollback_reason, operator=True)

    def _verify_rollback(
        self,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        artifact: ValidatedPatchArtifact,
        *,
        reason: str,
    ) -> CoordinatorResult:
        try:
            content, _ = self._journal.get_artifact(
                record.execution_id, ROLLBACK_VERIFICATION_ARTIFACT
            )
            verification = RecoveryVerification.model_validate_json(content)
        except KeyError:
            try:
                verification = self._request_recovery_verification(
                    plan, record, artifact, rollback=True
                )
            except SignedContractError as exc:
                failure = f"{reason}; Agent 1 rollback evidence rejected: {exc}"
                record = self._transition(
                    record,
                    ExecutionState.ROLLBACK_FAILED,
                    reason=self._reason(failure),
                )
                return self._result(record, failure, operator=True)
            self._persist_model(
                record.execution_id, ROLLBACK_VERIFICATION_ARTIFACT, verification
            )
        if verification.verdict == VerificationVerdict.VERIFIED:
            record = self._transition(
                record, ExecutionState.ROLLED_BACK, verification=verification
            )
            return self._result(
                record,
                f"{reason}; exact prior state restored and Agent 1 verified recovery",
            )
        failure = (
            f"{reason}; rollback recovery verification is "
            f"{verification.verdict.value.lower()}"
        )
        record = self._transition(
            record,
            ExecutionState.ROLLBACK_FAILED,
            verification=verification,
            reason=self._reason(failure),
        )
        return self._result(record, failure, operator=True)

    def _resume(
        self,
        operation: AuthorizedOperation,
        plan: DeploymentPlan,
        record: ExecutionRecord,
        *,
        signed_authorization: SignedAuthorizationEnvelopeV1 | None,
        cancelled: Callable[[], bool] | None,
    ) -> CoordinatorResult:
        if is_terminal(record.state):
            return self._result(
                record, "idempotent replay returned the existing terminal result"
            )
        if record.state in {
            ExecutionState.DRY_RUN_PASSED,
            ExecutionState.EXECUTING,
            ExecutionState.OUTCOME_UNCERTAIN,
        }:
            artifact = self._load_patch(record.execution_id, FORWARD_ARTIFACT)
            if record.state == ExecutionState.DRY_RUN_PASSED:
                try:
                    self._journal.get_artifact(
                        record.execution_id, EXTERNAL_AUTHORIZATION_ARTIFACT
                    )
                    reuse_external = True
                except KeyError:
                    reuse_external = False
                return self._apply_forward(
                    operation,
                    plan,
                    record,
                    artifact,
                    snapshot_generation=artifact.base_generation,
                    signed_authorization=signed_authorization,
                    cancelled=cancelled,
                    reuse_external_authorization=reuse_external,
                )
            if record.state == ExecutionState.EXECUTING:
                record = self._transition(record, ExecutionState.OUTCOME_UNCERTAIN)
            return self._reconcile_forward(
                operation,
                plan,
                record,
                artifact,
                expected_generation=artifact.base_generation + 1,
                uncertainty="process restarted with an in-flight mutation",
                cancelled=cancelled,
            )
        if record.state == ExecutionState.OBSERVING:
            artifact = self._load_patch(record.execution_id, FORWARD_ARTIFACT)
            snapshot = self._inspector.inspect_deployment(operation.target)
            return self._observe_and_verify(
                operation,
                plan,
                record,
                artifact,
                expected_generation=snapshot.generation,
                cancelled=cancelled,
            )
        if record.state == ExecutionState.AWAITING_AGENT1:
            artifact = self._load_patch(record.execution_id, FORWARD_ARTIFACT)
            return self._verify_agent1(operation, plan, record, artifact)
        if record.state == ExecutionState.ROLLBACK_PENDING:
            artifact = self._load_patch(record.execution_id, FORWARD_ARTIFACT)
            return self._rollback(
                operation,
                plan,
                record,
                artifact,
                reason="resuming a persisted rollback request",
            )
        if record.state == ExecutionState.ROLLBACK_AWAITING_AGENT1:
            rollback = self._load_patch(record.execution_id, ROLLBACK_ARTIFACT)
            return self._verify_rollback(
                plan,
                record,
                rollback,
                reason="resuming independent rollback recovery verification",
            )
        if record.state == ExecutionState.ROLLING_BACK:
            artifact = self._load_patch(record.execution_id, FORWARD_ARTIFACT)
            try:
                rollback = self._load_patch(record.execution_id, ROLLBACK_ARTIFACT)
                current = self._inspector.inspect_deployment(operation.target)
            except (KeyError, KubernetesAdapterError) as exc:
                return self._result(
                    record,
                    f"in-flight rollback remains uncertain after restart: {exc}",
                    operator=True,
                )
            if matches_exact_prior_state(artifact, current):
                try:
                    content, _ = self._journal.get_artifact(
                        record.execution_id, ROLLBACK_ROLLOUT_ARTIFACT
                    )
                    rollout = RolloutObservationResult.model_validate_json(content)
                except KeyError:
                    rollout = self._observer.observe(
                        self._inspector,
                        operation.target,
                        expected_generation=rollback.base_generation + 1,
                    )
                    self._persist_model(
                        record.execution_id, ROLLBACK_ROLLOUT_ARTIFACT, rollout
                    )
                if rollout.outcome != RolloutOutcome.SUCCEEDED:
                    failure = (
                        "restarted rollback did not produce successful rollout evidence: "
                        f"{rollout.reason}"
                    )
                    record = self._transition(
                        record,
                        ExecutionState.ROLLBACK_FAILED,
                        reason=self._reason(failure),
                    )
                    return self._result(record, failure, operator=True)
                record = self._transition(
                    record, ExecutionState.ROLLBACK_AWAITING_AGENT1
                )
                return self._verify_rollback(
                    plan,
                    record,
                    rollback,
                    reason="resumed exact rollback after process restart",
                )
            return self._result(
                record,
                "in-flight rollback did not prove exact prior state and was not retried",
                operator=True,
            )
        return self._result(
            record,
            "execution cannot be automatically resumed from this state without ambiguity",
            operator=True,
        )
