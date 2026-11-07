"""Public-key verification for Phase 4 contracts.

Runtime code contains no signer, private key, self-issued approval, or fallback
test key. Ed25519 signing helpers belong exclusively to tests/external issuers.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import SecretStr, ValidationError, model_validator

from agent4_deployment_recovery.core.canonical import canonical_json, canonical_sha256
from agent4_deployment_recovery.core.errors import (
    ApprovalProvenanceError,
    SignatureVerificationError,
    SignedContractError,
)
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    ApprovalProvenanceDecision,
    ApprovalProvenanceReference,
    AuthorizationBinding,
    RecoveryVerificationRequestV1,
    SignedAuthorizationEnvelopeV1,
    SignedRecoveryVerificationEnvelopeV1,
    VerifiedExternalAuthorization,
)
from agent4_deployment_recovery.core.schemas import (
    Identifier,
    NonEmptyStr,
    RecoveryVerification,
    StrictModel,
)

MAX_CLOCK_SKEW = timedelta(minutes=2)
MAX_VERDICT_AGE = timedelta(minutes=15)


def _utc_runtime(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SignedContractError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class TrustedIssuerKey(StrictModel):
    key_id: Identifier
    issuer: Identifier
    audience: NonEmptyStr
    public_key_pem: SecretStr


class TrustedIssuerRegistry(StrictModel):
    keys: tuple[TrustedIssuerKey, ...]

    @model_validator(mode="after")
    def validate_unique_keys(self):
        if not self.keys:
            raise ValueError("at least one trusted issuer key is required")
        identities = tuple(item.key_id for item in self.keys)
        if len(set(identities)) != len(identities):
            raise ValueError("trusted issuer key IDs must be unique")
        return self

    def key(self, key_id: str) -> TrustedIssuerKey:
        matches = tuple(item for item in self.keys if item.key_id == key_id)
        if len(matches) != 1:
            raise SignatureVerificationError("message key_id is not trusted")
        return matches[0]


@runtime_checkable
class TrustedMessageReplayStore(Protocol):
    def consume_trusted_message(
        self,
        *,
        message_kind: str,
        message_id: str,
        execution_id: str,
        issuer: str,
        claims_hash: str,
        now: datetime,
    ) -> bool:
        """Persist a one-time message ID or reject a replay."""

    def reserve_authorization_permit(
        self,
        *,
        permit_id: str,
        execution_id: str,
        operation_id: str,
        plan_hash: str,
        issuer: str,
        claims_hash: str,
        authorization: VerifiedExternalAuthorization,
        now: datetime,
    ) -> bool:
        """Atomically consume and bind a permit to its execution artifact."""


@runtime_checkable
class ApprovalProvenanceVerifier(Protocol):
    def verify(
        self,
        reference: ApprovalProvenanceReference,
        *,
        binding: AuthorizationBinding,
    ) -> ApprovalProvenanceDecision:
        """Authenticate a receipt against the entire expected execution scope."""


class DenyAllApprovalProvenanceVerifier:
    def verify(
        self,
        reference: ApprovalProvenanceReference,
        *,
        binding: AuthorizationBinding,
    ) -> ApprovalProvenanceDecision:
        del reference, binding
        return ApprovalProvenanceDecision(
            trusted=False,
            verifier_id="approval-provenance-unconfigured",
            checked_at=datetime.now(UTC),
            reason="no trusted HITL approval provenance verifier is configured",
        )


def _decode_signature(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise SignatureVerificationError("signature is not valid base64url") from exc


class Ed25519EnvelopeVerifier:
    def __init__(self, registry: TrustedIssuerRegistry):
        self._registry = registry

    def verify(
        self, *, key_id: str, issuer: str, audience: str, payload, signature: str
    ) -> str:
        trusted = self._registry.key(key_id)
        if trusted.issuer != issuer:
            raise SignatureVerificationError(
                "signed issuer does not match trusted key owner"
            )
        if trusted.audience != audience:
            raise SignatureVerificationError(
                "signed audience is not trusted for this key"
            )
        try:
            public_key = serialization.load_pem_public_key(
                trusted.public_key_pem.get_secret_value().encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise SignatureVerificationError("trusted public key is malformed") from exc
        if not isinstance(public_key, Ed25519PublicKey):
            raise SignatureVerificationError("trusted key is not Ed25519")
        material = canonical_json(payload).encode("utf-8")
        try:
            public_key.verify(_decode_signature(signature), material)
        except InvalidSignature as exc:
            raise SignatureVerificationError(
                "Ed25519 signature verification failed"
            ) from exc
        return canonical_sha256(payload)


class SignedAuthorizationPermitVerifier:
    """Authenticate an external permit and independently verify HITL provenance."""

    verifier_id = "ed25519-external-authorization-v1"

    def __init__(
        self,
        registry: TrustedIssuerRegistry,
        *,
        approval_verifier: ApprovalProvenanceVerifier | None = None,
        replay_store: TrustedMessageReplayStore,
    ):
        self._signatures = Ed25519EnvelopeVerifier(registry)
        self._approval = approval_verifier or DenyAllApprovalProvenanceVerifier()
        self._replays = replay_store

    def verify_and_consume(
        self,
        envelope: SignedAuthorizationEnvelopeV1,
        binding: AuthorizationBinding,
        *,
        execution_id: str,
        at: datetime,
    ) -> VerifiedExternalAuthorization:
        try:
            envelope = SignedAuthorizationEnvelopeV1.model_validate(envelope)
        except ValidationError as exc:
            raise SignedContractError(
                "authorization envelope is invalid or unsigned"
            ) from exc
        current = _utc_runtime(at, "authorization verification time")
        claims = envelope.claims
        claims_hash = self._signatures.verify(
            key_id=envelope.key_id,
            issuer=claims.issuer,
            audience=claims.audience,
            payload=claims,
            signature=envelope.signature,
        )
        if claims.issued_at > current + MAX_CLOCK_SKEW:
            raise SignedContractError("authorization permit was issued in the future")
        if current < claims.not_before:
            raise SignedContractError("authorization permit is not active yet")
        if current >= claims.expires_at:
            raise SignedContractError("authorization permit is expired")
        expected = (
            binding.audience,
            binding.operation_id,
            binding.correlation_id,
            binding.environment,
            binding.target,
            binding.operation,
            binding.plan_hash,
            binding.patch_hash,
            binding.authorization_scope_hash,
            binding.rollback_scope,
        )
        actual = (
            claims.audience,
            claims.operation_id,
            claims.correlation_id,
            claims.environment,
            claims.target,
            claims.operation,
            claims.plan_hash,
            claims.patch_hash,
            claims.authorization_scope_hash,
            claims.rollback_scope,
        )
        if actual != expected:
            raise SignedContractError(
                "authorization permit scope does not match execution"
            )
        if claims.approval.method != ApprovalMethod.HUMAN_INTERACTIVE:
            raise ApprovalProvenanceError(
                "live mutation requires authenticated human approval provenance"
            )
        approval = self._approval.verify(
            claims.approval,
            binding=binding,
        )
        if not approval.trusted or approval.reference is None:
            raise ApprovalProvenanceError(approval.reason)
        verified = VerifiedExternalAuthorization(
            permit_id=claims.permit_id,
            issuer=claims.issuer,
            verifier_id=self.verifier_id,
            claims_hash=claims_hash,
            approval_reference=approval.reference,
            checked_at=current,
            expires_at=claims.expires_at,
        )
        self._replays.reserve_authorization_permit(
            permit_id=claims.permit_id,
            execution_id=execution_id,
            operation_id=claims.operation_id,
            plan_hash=claims.plan_hash,
            issuer=claims.issuer,
            claims_hash=claims_hash,
            authorization=verified,
            now=current,
        )
        return verified


@runtime_checkable
class RecoveryVerdictProvider(Protocol):
    def get_verdict(
        self, request: RecoveryVerificationRequestV1
    ) -> SignedRecoveryVerificationEnvelopeV1:
        """Fetch one signed Agent 1 verdict for an exact request."""


class SignedAgent1RecoveryVerifier:
    """Validate signed Agent 1 evidence; Kubernetes readiness is never consulted."""

    verifier_id = "ed25519-agent1-verification-v1"

    def __init__(
        self,
        registry: TrustedIssuerRegistry,
        *,
        replay_store: TrustedMessageReplayStore,
        provider: RecoveryVerdictProvider,
        now=None,
    ):
        self._signatures = Ed25519EnvelopeVerifier(registry)
        self._replays = replay_store
        self._provider = provider
        self._now = now or (lambda: datetime.now(UTC))

    def verify_bound(
        self, request: RecoveryVerificationRequestV1
    ) -> RecoveryVerification:
        raw_envelope = self._provider.get_verdict(request)
        try:
            envelope = SignedRecoveryVerificationEnvelopeV1.model_validate(raw_envelope)
        except ValidationError as exc:
            raise SignedContractError("Agent 1 verdict is invalid or unsigned") from exc
        current = _utc_runtime(self._now(), "Agent 1 verification time")
        claims = envelope.claims
        claims_hash = self._signatures.verify(
            key_id=envelope.key_id,
            issuer=claims.issuer,
            audience=claims.audience,
            payload=claims,
            signature=envelope.signature,
        )
        expected = (
            request.audience,
            request.request_id,
            request.requested_at,
            request.execution_id,
            request.correlation_id,
            request.target_uid,
            request.expected_plan_hash,
            request.expected_patch_hash,
            request.expected_image,
            request.expected_version,
        )
        actual = (
            claims.audience,
            claims.request_id,
            claims.verification_requested_at,
            claims.execution_id,
            claims.correlation_id,
            claims.target_uid,
            claims.expected_plan_hash,
            claims.expected_patch_hash,
            claims.expected_image,
            claims.expected_version,
        )
        if actual != expected:
            raise SignedContractError(
                "Agent 1 verdict does not match verification request"
            )
        if claims.issued_at > current + MAX_CLOCK_SKEW:
            raise SignedContractError("Agent 1 verdict was issued in the future")
        if current >= claims.expires_at:
            raise SignedContractError("Agent 1 verdict is expired")
        if current - claims.issued_at > MAX_VERDICT_AGE:
            raise SignedContractError("Agent 1 verdict is stale")
        self._replays.consume_trusted_message(
            message_kind="AGENT1_VERDICT",
            message_id=claims.verdict_id,
            execution_id=request.execution_id,
            issuer=claims.issuer,
            claims_hash=claims_hash,
            now=current,
        )
        return RecoveryVerification(
            verification_id=claims.verdict_id,
            execution_id=claims.execution_id,
            correlation_id=claims.correlation_id,
            verifier_id=self.verifier_id,
            verdict=claims.verdict,
            observed_at=claims.observation_ended_at,
            target_uid=claims.target_uid,
            observed_version=(
                claims.expected_image
                or claims.expected_version
                or claims.expected_patch_hash
            ),
            evidence_refs=claims.evidence_refs,
            detail=claims.detail,
            signature=f"ed25519:{envelope.signature}",
        )

    def verify(self, plan, execution):
        del plan, execution
        raise SignedContractError(
            "signed Agent 1 verification requires a persisted bound request"
        )
