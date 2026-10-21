import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent4_deployment_recovery.adapters.signed_contracts import (
    SignedAgent1RecoveryVerifier,
    SignedAuthorizationPermitVerifier,
    TrustedIssuerKey,
    TrustedIssuerRegistry,
)
from agent4_deployment_recovery.core.canonical import canonical_json
from agent4_deployment_recovery.core.errors import (
    ApprovalProvenanceError,
    MessageReplayError,
    SignatureVerificationError,
    SignedContractError,
)
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    ApprovalProvenanceDecision,
    ApprovalProvenanceReference,
    AuthorizationBinding,
    ExternalAuthorizationClaimsV1,
    RecoveryVerificationClaimsV1,
    RecoveryVerificationRequestV1,
    SignedAuthorizationEnvelopeV1,
    SignedRecoveryVerificationEnvelopeV1,
)
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import VerificationVerdict
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def _keys(*, issuer="trusted-issuer", audience="agent4-deployment-recovery"):
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    registry = TrustedIssuerRegistry(
        keys=(
            TrustedIssuerKey(
                key_id="KEY-001",
                issuer=issuer,
                audience=audience,
                public_key_pem=public,
            ),
        )
    )
    return private, registry


def _signature(private, model):
    raw = private.sign(canonical_json(model).encode())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TrustedApproval:
    def __init__(self, trusted=True):
        self.trusted = trusted

    def verify(self, reference, *, binding):
        del reference, binding
        return ApprovalProvenanceDecision(
            trusted=self.trusted,
            verifier_id="trusted-hitl-verifier",
            checked_at=datetime.now(UTC),
            reference="HITL-RECEIPT-001" if self.trusted else None,
            reason="trusted receipt" if self.trusted else "receipt mismatch",
        )


def _authorization_material(tmp_path, operation_factory):
    operation = operation_factory()
    plan = create_plan(operation)
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    journal.create_execution(plan, execution_id="EXEC-001", approval_id="APR-001")
    now = datetime.now(UTC)
    patch_hash = "a" * 64
    claims = ExternalAuthorizationClaimsV1(
        permit_id="PERMIT-001",
        issuer="trusted-issuer",
        audience="agent4-deployment-recovery",
        issued_at=operation.issued_at,
        not_before=operation.issued_at,
        expires_at=now + timedelta(minutes=10),
        operation_id=operation.operation_id,
        correlation_id=operation.correlation_id,
        environment=operation.environment,
        target=operation.target,
        operation=operation.operation,
        plan_hash=plan.plan_hash,
        patch_hash=patch_hash,
        authorization_scope_hash=plan.authorization_scope_hash,
        rollback_scope=plan.rollback_scope,
        approval=ApprovalProvenanceReference(
            provider_id="hitl-provider",
            receipt_id="RECEIPT-001",
            approver_subject="operator@example.test",
            method=ApprovalMethod.HUMAN_INTERACTIVE,
            evidence_hash="b" * 64,
            approved_at=operation.issued_at,
        ),
    )
    binding = AuthorizationBinding(
        audience=claims.audience,
        operation_id=claims.operation_id,
        correlation_id=claims.correlation_id,
        environment=claims.environment,
        target=claims.target,
        operation=claims.operation,
        plan_hash=claims.plan_hash,
        patch_hash=claims.patch_hash,
        authorization_scope_hash=claims.authorization_scope_hash,
        rollback_scope=claims.rollback_scope,
    )
    return journal, claims, binding, now


def test_signed_authorization_rejects_forgery_unknown_issuer_and_replay(
    tmp_path, operation_factory
):
    journal, claims, binding, now = _authorization_material(tmp_path, operation_factory)
    private, registry = _keys()
    verifier = SignedAuthorizationPermitVerifier(
        registry, approval_verifier=TrustedApproval(), replay_store=journal
    )
    envelope = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )
    verified = verifier.verify_and_consume(
        envelope, binding, execution_id="EXEC-001", at=now
    )
    assert verified.permit_id == "PERMIT-001"
    verifier = SignedAuthorizationPermitVerifier(
        registry,
        approval_verifier=TrustedApproval(),
        replay_store=SQLiteExecutionJournal(journal.path),
    )
    with pytest.raises(MessageReplayError):
        verifier.verify_and_consume(envelope, binding, execution_id="EXEC-001", at=now)

    forged = envelope.model_copy(update={"signature": "A" * 86})
    with pytest.raises(SignatureVerificationError):
        verifier.verify_and_consume(forged, binding, execution_id="EXEC-001", at=now)

    wrong_claims = claims.model_copy(update={"issuer": "untrusted-issuer"})
    wrong = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001",
        claims=wrong_claims,
        signature=_signature(private, wrong_claims),
    )
    with pytest.raises(SignatureVerificationError):
        verifier.verify_and_consume(wrong, binding, execution_id="EXEC-001", at=now)

    wrong_audience_registry = TrustedIssuerRegistry(
        keys=(registry.keys[0].model_copy(update={"audience": "another-service"}),)
    )
    wrong_audience = SignedAuthorizationPermitVerifier(
        wrong_audience_registry,
        approval_verifier=TrustedApproval(),
        replay_store=journal,
    )
    with pytest.raises(SignatureVerificationError, match="audience"):
        wrong_audience.verify_and_consume(
            envelope, binding, execution_id="EXEC-001", at=now
        )


def test_signed_authorization_rejects_expiry_scope_and_untrusted_hitl(
    tmp_path, operation_factory
):
    journal, claims, binding, now = _authorization_material(tmp_path, operation_factory)
    private, registry = _keys()
    verifier = SignedAuthorizationPermitVerifier(
        registry, approval_verifier=TrustedApproval(), replay_store=journal
    )
    expired = claims.model_copy(
        update={
            "issued_at": now - timedelta(minutes=2),
            "not_before": now - timedelta(minutes=2),
            "expires_at": now - timedelta(seconds=1),
            "approval": claims.approval.model_copy(
                update={"approved_at": now - timedelta(minutes=3)}
            ),
        }
    )
    expired_envelope = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=expired, signature=_signature(private, expired)
    )
    with pytest.raises(SignedContractError, match="expired"):
        verifier.verify_and_consume(
            expired_envelope, binding, execution_id="EXEC-001", at=now
        )

    envelope = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )
    for field in ("patch_hash", "plan_hash"):
        wrong_binding = binding.model_copy(update={field: "f" * 64})
        with pytest.raises(SignedContractError, match="scope"):
            verifier.verify_and_consume(
                envelope, wrong_binding, execution_id="EXEC-001", at=now
            )
    untrusted = SignedAuthorizationPermitVerifier(
        registry, approval_verifier=TrustedApproval(False), replay_store=journal
    )
    with pytest.raises(ApprovalProvenanceError):
        untrusted.verify_and_consume(envelope, binding, execution_id="EXEC-001", at=now)
    invalid_schema = envelope.model_dump(mode="json")
    invalid_schema["schema_version"] = "2.0"
    with pytest.raises(ValueError):
        SignedAuthorizationEnvelopeV1.model_validate(invalid_schema)


def test_signed_authorization_denies_when_hitl_verifier_is_missing(
    tmp_path, operation_factory
):
    journal, claims, binding, now = _authorization_material(tmp_path, operation_factory)
    private, registry = _keys()
    verifier = SignedAuthorizationPermitVerifier(registry, replay_store=journal)
    envelope = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )
    with pytest.raises(ApprovalProvenanceError, match="no trusted HITL"):
        verifier.verify_and_consume(envelope, binding, execution_id="EXEC-001", at=now)


def test_authorization_reservation_rolls_back_replay_and_artifact_together(
    tmp_path, operation_factory, monkeypatch
):
    journal, claims, binding, now = _authorization_material(tmp_path, operation_factory)
    private, registry = _keys()
    verifier = SignedAuthorizationPermitVerifier(
        registry, approval_verifier=TrustedApproval(), replay_store=journal
    )
    envelope = SignedAuthorizationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )
    original = journal._append_outbox_event

    def fail_outbox(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated crash before authorization commit")

    monkeypatch.setattr(journal, "_append_outbox_event", fail_outbox)
    with pytest.raises(RuntimeError, match="simulated crash"):
        verifier.verify_and_consume(envelope, binding, execution_id="EXEC-001", at=now)
    with pytest.raises(KeyError):
        journal.get_artifact("EXEC-001", "phase4.external_authorization")

    monkeypatch.setattr(journal, "_append_outbox_event", original)
    verified = verifier.verify_and_consume(
        envelope, binding, execution_id="EXEC-001", at=now
    )
    assert verified.permit_id == claims.permit_id


class VerdictProvider:
    def __init__(self, envelope):
        self.envelope = envelope

    def get_verdict(self, request):
        del request
        return self.envelope


class UnsignedVerdictProvider:
    def get_verdict(self, request):
        del request
        return {"claims": {"verdict": "VERIFIED"}}


def _signed_verdict(
    private,
    request,
    *,
    now,
    target_uid=None,
    issued_at=None,
    verdict=VerificationVerdict.VERIFIED,
):
    issued = issued_at or now
    claims = RecoveryVerificationClaimsV1(
        verdict_id="VERDICT-001",
        issuer="agent1-trusted",
        audience=request.audience,
        issued_at=issued,
        expires_at=now + timedelta(minutes=5),
        request_id=request.request_id,
        verification_requested_at=request.requested_at,
        observation_started_at=request.requested_at,
        observation_ended_at=issued,
        execution_id=request.execution_id,
        correlation_id=request.correlation_id,
        target_uid=target_uid or request.target_uid,
        expected_plan_hash=request.expected_plan_hash,
        expected_patch_hash=request.expected_patch_hash,
        expected_image=request.expected_image,
        expected_version=request.expected_version,
        verdict=verdict,
        evidence_refs=("agent1://observation/001",),
        detail="independent health evidence passed",
    )
    return SignedRecoveryVerificationEnvelopeV1(
        key_id="KEY-001", claims=claims, signature=_signature(private, claims)
    )


def test_agent1_verdict_is_bound_and_one_time(tmp_path, operation_factory):
    journal, _, _, now = _authorization_material(tmp_path, operation_factory)
    request = RecoveryVerificationRequestV1(
        request_id="VERIFY-REQ-001",
        audience="agent4-deployment-recovery",
        execution_id="EXEC-001",
        correlation_id="CORR-001",
        target_uid="uid-fraud-api-001",
        expected_plan_hash="a" * 64,
        expected_patch_hash="b" * 64,
        expected_image="registry.example/fraud-api@sha256:" + "c" * 64,
        requested_at=now - timedelta(seconds=10),
    )
    private, registry = _keys(
        issuer="agent1-trusted", audience="agent4-deployment-recovery"
    )
    envelope = _signed_verdict(private, request, now=now)
    verifier = SignedAgent1RecoveryVerifier(
        registry,
        replay_store=journal,
        provider=VerdictProvider(envelope),
        now=lambda: now,
    )
    assert verifier.verify_bound(request).verdict == VerificationVerdict.VERIFIED
    with pytest.raises(MessageReplayError):
        verifier.verify_bound(request)


@pytest.mark.parametrize(
    "verdict", (VerificationVerdict.FAILED, VerificationVerdict.INCONCLUSIVE)
)
def test_agent1_signed_non_success_verdicts_remain_non_success(
    tmp_path, operation_factory, verdict
):
    journal, _, _, now = _authorization_material(tmp_path, operation_factory)
    request = RecoveryVerificationRequestV1(
        request_id="VERIFY-REQ-NON-SUCCESS",
        audience="agent4-deployment-recovery",
        execution_id="EXEC-001",
        correlation_id="CORR-001",
        target_uid="uid-fraud-api-001",
        expected_plan_hash="a" * 64,
        expected_patch_hash="b" * 64,
        expected_version="12346",
        requested_at=now - timedelta(seconds=10),
    )
    private, registry = _keys(
        issuer="agent1-trusted", audience="agent4-deployment-recovery"
    )
    envelope = _signed_verdict(private, request, now=now, verdict=verdict)
    verifier = SignedAgent1RecoveryVerifier(
        registry,
        replay_store=journal,
        provider=VerdictProvider(envelope),
        now=lambda: now,
    )
    assert verifier.verify_bound(request).verdict == verdict


def test_agent1_rejects_stale_and_target_mismatched_verdicts(
    tmp_path, operation_factory
):
    journal, _, _, now = _authorization_material(tmp_path, operation_factory)
    request = RecoveryVerificationRequestV1(
        request_id="VERIFY-REQ-002",
        audience="agent4-deployment-recovery",
        execution_id="EXEC-001",
        correlation_id="CORR-001",
        target_uid="uid-fraud-api-001",
        expected_plan_hash="a" * 64,
        expected_patch_hash="b" * 64,
        expected_version="12346",
        requested_at=now - timedelta(minutes=21),
    )
    private, registry = _keys(
        issuer="agent1-trusted", audience="agent4-deployment-recovery"
    )
    stale = _signed_verdict(
        private, request, now=now, issued_at=now - timedelta(minutes=20)
    )
    verifier = SignedAgent1RecoveryVerifier(
        registry,
        replay_store=journal,
        provider=VerdictProvider(stale),
        now=lambda: now,
    )
    with pytest.raises(SignedContractError, match="stale"):
        verifier.verify_bound(request)

    mismatch = _signed_verdict(
        private, request, now=now, target_uid="different-workload-uid"
    )
    verifier = SignedAgent1RecoveryVerifier(
        registry,
        replay_store=journal,
        provider=VerdictProvider(mismatch),
        now=lambda: now,
    )
    with pytest.raises(SignedContractError, match="does not match"):
        verifier.verify_bound(request)

    verifier = SignedAgent1RecoveryVerifier(
        registry,
        replay_store=journal,
        provider=UnsignedVerdictProvider(),
        now=lambda: now,
    )
    with pytest.raises(SignedContractError, match="unsigned"):
        verifier.verify_bound(request)
