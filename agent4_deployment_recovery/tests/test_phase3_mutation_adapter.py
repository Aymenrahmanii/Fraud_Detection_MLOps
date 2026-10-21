from datetime import UTC, datetime

import pytest
from kubernetes.client.exceptions import ApiException

from agent4_deployment_recovery.adapters.kubernetes_mutation import (
    DisabledKubernetesMutationAdapter,
    OfficialControlledMutationAdapter,
)
from agent4_deployment_recovery.core.errors import (
    KubernetesConflictError,
    KubernetesForbiddenError,
    KubernetesMalformedResponseError,
    KubernetesNotFoundError,
    MutationDisabledError,
)
from agent4_deployment_recovery.core.execution_models import (
    LiveMutationPermit,
    MutationOutcome,
    PatchDirection,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    DryRunDecision,
    ServerDryRunResult,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.recovery import build_forward_artifact


class FakeAppsApi:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def patch_namespaced_deployment(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"metadata": {"resourceVersion": "12346", "generation": 5}}


def _artifact(operation, snapshot, policy):
    plan = create_plan(operation)
    preflight = run_preflight(
        operation, snapshot, policy, requested_strategy=plan.strategy
    )
    preview = build_typed_patch(operation, snapshot, preflight)
    dry_run = ServerDryRunResult(
        decision=DryRunDecision.PASS,
        admission_accepted=True,
        operation_id=operation.operation_id,
        patch_hash=preview.patch_hash,
        response_uid=snapshot.uid,
        response_resource_version=snapshot.resource_version,
        reason="accepted",
    )
    return plan, build_forward_artifact(plan, snapshot, preflight, preview, dry_run)


def _permit(operation, plan, artifact, verifier="trusted-test-verifier"):
    return LiveMutationPermit(
        operation_id=operation.operation_id,
        plan_hash=plan.plan_hash,
        authorization_scope_hash=plan.authorization_scope_hash,
        rollback_scope_hash=artifact.rollback_scope_hash,
        patch_bytes_hash=artifact.patch_bytes_hash,
        direction=PatchDirection.FORWARD,
        verifier_id=verifier,
        authorization_reference="AUTH-test-reference",
        environment="development",
        external_permit_id="PERMIT-test-reference",
        external_claims_hash="c" * 64,
        approval_provenance_reference="HITL-test-reference",
        checked_at=datetime.now(UTC),
    )


def _adapter(snapshot, apps_api, policy, *, enabled=True):
    return OfficialControlledMutationAdapter(
        identity=snapshot.cluster,
        request_timeout=(1, 2),
        apps_api=apps_api,
        allowed_targets=policy.allowed_targets,
        allowed_environments=frozenset({"development"}),
        trusted_verifier_ids=frozenset({"trusted-test-verifier"}),
        enabled=enabled,
    )


def test_default_mutation_adapter_always_denies(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    plan, artifact = _artifact(operation, snapshot_factory(), preflight_policy)
    with pytest.raises(MutationDisabledError):
        DisabledKubernetesMutationAdapter().apply_validated_patch(
            operation.target, artifact, _permit(operation, plan, artifact)
        )


def test_controlled_adapter_uses_exact_body_and_has_no_dry_run_flag(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    api = FakeAppsApi()
    result = _adapter(snapshot, api, preflight_policy).apply_validated_patch(
        operation.target, artifact, _permit(operation, plan, artifact)
    )
    assert result.outcome == MutationOutcome.APPLIED
    assert api.calls[0]["body"] == [
        item.model_dump(mode="json", exclude_none=True) for item in artifact.patch
    ]
    assert "dry_run" not in api.calls[0]
    assert set(api.calls[0]) == {
        "name",
        "namespace",
        "body",
        "field_manager",
        "field_validation",
        "_request_timeout",
    }


def test_controlled_adapter_rejects_disabled_untrusted_and_wrong_hash(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    api = FakeAppsApi()
    with pytest.raises(MutationDisabledError, match="disabled"):
        _adapter(snapshot, api, preflight_policy, enabled=False).apply_validated_patch(
            operation.target, artifact, _permit(operation, plan, artifact)
        )
    with pytest.raises(MutationDisabledError, match="trusted"):
        _adapter(snapshot, api, preflight_policy).apply_validated_patch(
            operation.target,
            artifact,
            _permit(operation, plan, artifact, verifier="untrusted-verifier"),
        )
    wrong = _permit(operation, plan, artifact).model_copy(
        update={"patch_bytes_hash": "0" * 64}
    )
    with pytest.raises(MutationDisabledError, match="exact persisted"):
        _adapter(snapshot, api, preflight_policy).apply_validated_patch(
            operation.target, artifact, wrong
        )
    assert api.calls == []


def test_controlled_adapter_rejects_unsigned_in_process_permit(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    api = FakeAppsApi()
    unsigned = _permit(operation, plan, artifact).model_copy(
        update={
            "external_permit_id": None,
            "external_claims_hash": None,
            "approval_provenance_reference": None,
        }
    )
    with pytest.raises(MutationDisabledError, match="signed external"):
        _adapter(snapshot, api, preflight_policy).apply_validated_patch(
            operation.target, artifact, unsigned
        )
    assert api.calls == []


def test_mutation_timeout_is_uncertain_and_conflict_is_not_retried(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    timeout_api = FakeAppsApi(ApiException(status=504, reason="gateway timeout"))
    timeout = _adapter(snapshot, timeout_api, preflight_policy).apply_validated_patch(
        operation.target, artifact, _permit(operation, plan, artifact)
    )
    assert timeout.outcome == MutationOutcome.UNCERTAIN
    assert len(timeout_api.calls) == 1

    conflict_api = FakeAppsApi(ApiException(status=409, reason="conflict"))
    with pytest.raises(KubernetesConflictError):
        _adapter(snapshot, conflict_api, preflight_policy).apply_validated_patch(
            operation.target, artifact, _permit(operation, plan, artifact)
        )
    assert len(conflict_api.calls) == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, KubernetesForbiddenError), (404, KubernetesNotFoundError)],
)
def test_mutation_forbidden_and_not_found_fail_closed(
    operation_factory,
    snapshot_factory,
    preflight_policy,
    status,
    error_type,
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    api = FakeAppsApi(ApiException(status=status, reason="injected API rejection"))
    with pytest.raises(error_type):
        _adapter(snapshot, api, preflight_policy).apply_validated_patch(
            operation.target, artifact, _permit(operation, plan, artifact)
        )
    assert len(api.calls) == 1


def test_ambiguous_mutation_response_fails_closed(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    plan, artifact = _artifact(operation, snapshot, preflight_policy)
    api = FakeAppsApi()
    api.patch_namespaced_deployment = lambda **kwargs: {"metadata": {}}
    with pytest.raises(KubernetesMalformedResponseError):
        _adapter(snapshot, api, preflight_policy).apply_validated_patch(
            operation.target, artifact, _permit(operation, plan, artifact)
        )
