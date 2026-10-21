"""Agent 1 verification interface; no Agent 1 implementation is fabricated."""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.integration_models import (
    RecoveryVerificationRequestV1,
)
from agent4_deployment_recovery.core.schemas import (
    DeploymentPlan,
    ExecutionRecord,
    RecoveryVerification,
    VerificationVerdict,
)


@runtime_checkable
class RecoveryVerifier(Protocol):
    def verify(
        self, plan: DeploymentPlan, execution: ExecutionRecord
    ) -> RecoveryVerification:
        """Return independently observed recovery evidence."""


class UnavailableRecoveryVerifier:
    """Fail-closed placeholder used until a trusted Agent 1 verifier exists."""

    verifier_id = "agent1-verifier-unconfigured"

    def verify_bound(
        self, request: RecoveryVerificationRequestV1
    ) -> RecoveryVerification:
        return RecoveryVerification(
            verification_id=f"VERIFY-UNAVAILABLE-{canonical_sha256(request.request_id)[:20]}",
            execution_id=request.execution_id,
            correlation_id=request.correlation_id,
            verifier_id=self.verifier_id,
            verdict=VerificationVerdict.UNAVAILABLE,
            observed_at=datetime.now(UTC),
            target_uid=request.target_uid,
            observed_version=(
                request.expected_image
                or request.expected_version
                or request.expected_patch_hash
            ),
            detail="no trusted Agent 1 recovery verifier is configured",
        )

    def verify(
        self, plan: DeploymentPlan, execution: ExecutionRecord
    ) -> RecoveryVerification:
        return RecoveryVerification(
            verification_id=f"VERIFY-UNAVAILABLE-{canonical_sha256(execution.execution_id)[:20]}",
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            verifier_id=self.verifier_id,
            verdict=VerificationVerdict.UNAVAILABLE,
            observed_at=datetime.now(UTC),
            target_uid=plan.target.uid,
            detail="no trusted Agent 1 recovery verifier is configured",
        )
