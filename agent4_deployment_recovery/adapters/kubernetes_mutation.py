"""Narrow Phase 3 Kubernetes mutation boundary.

This module is intentionally not wired into the public CLI. The production
adapter accepts only a persisted typed artifact plus a freshly bound permit.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from agent4_deployment_recovery.adapters.kubernetes import _map_api_error
from agent4_deployment_recovery.core.errors import (
    KubernetesConnectivityError,
    KubernetesMalformedResponseError,
    KubernetesTimeoutError,
    MutationDisabledError,
)
from agent4_deployment_recovery.core.execution_models import (
    LiveMutationPermit,
    MutationOutcome,
    MutationResult,
    ValidatedPatchArtifact,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    ClusterIdentity,
)
from agent4_deployment_recovery.core.schemas import KubernetesTarget

MAX_PERMIT_AGE = timedelta(seconds=30)


@runtime_checkable
class KubernetesMutationAdapter(Protocol):
    def apply_validated_patch(
        self,
        target: KubernetesTarget,
        artifact: ValidatedPatchArtifact,
        permit: LiveMutationPermit,
    ) -> MutationResult:
        """Apply only a previously persisted, hash-bound typed patch."""


class DisabledKubernetesMutationAdapter:
    """Safe default: no live call can be reached."""

    def apply_validated_patch(
        self,
        target: KubernetesTarget,
        artifact: ValidatedPatchArtifact,
        permit: LiveMutationPermit,
    ) -> MutationResult:
        del target, artifact, permit
        raise MutationDisabledError("live Kubernetes mutation is not configured")


class OfficialControlledMutationAdapter:
    """Official-client adapter limited to allowlisted namespaced Deployments.

    Construction requires an already configured API client, an explicit
    allowlist, trusted verifier IDs, and ``enabled=True``. There is deliberately
    no ambient-config factory and no CLI route to this class.
    """

    def __init__(
        self,
        *,
        identity: ClusterIdentity,
        request_timeout: tuple[float, float],
        apps_api: Any,
        allowed_targets: tuple[AllowedDeploymentTarget, ...],
        allowed_environments: frozenset[str],
        trusted_verifier_ids: frozenset[str],
        enabled: bool = False,
    ):
        if not allowed_targets:
            raise MutationDisabledError(
                "live mutation requires an explicit target allowlist"
            )
        if not trusted_verifier_ids:
            raise MutationDisabledError("live mutation requires trusted verifier IDs")
        if not allowed_environments:
            raise MutationDisabledError(
                "live mutation requires an environment allowlist"
            )
        self._identity = identity
        self._request_timeout = request_timeout
        self._apps_api = apps_api
        self._allowed_targets = frozenset(allowed_targets)
        self._trusted_verifier_ids = trusted_verifier_ids
        self._allowed_environments = allowed_environments
        self._enabled = enabled

    def _validate(
        self,
        target: KubernetesTarget,
        artifact: ValidatedPatchArtifact,
        permit: LiveMutationPermit,
    ) -> None:
        if not self._enabled:
            raise MutationDisabledError("live mutation adapter is disabled")
        allowed = AllowedDeploymentTarget(
            cluster_id=target.cluster_id,
            namespace=target.namespace,
            workload=target.name,
            container=target.container,
        )
        if (
            target.cluster_id != self._identity.cluster_id
            or allowed not in self._allowed_targets
        ):
            raise MutationDisabledError(
                "target is outside the configured mutation allowlist"
            )
        if artifact.target_uid != target.uid:
            raise MutationDisabledError(
                "artifact target UID does not match immutable target"
            )
        if permit.verifier_id not in self._trusted_verifier_ids:
            raise MutationDisabledError(
                "permit was not issued by a configured trusted verifier"
            )
        if permit.environment not in self._allowed_environments:
            raise MutationDisabledError(
                "environment is outside the live mutation allowlist"
            )
        if (
            permit.external_permit_id is None
            or permit.external_claims_hash is None
            or permit.approval_provenance_reference is None
        ):
            raise MutationDisabledError(
                "live mutation requires signed external authorization and HITL provenance"
            )
        if (
            permit.operation_id != artifact.operation_id
            or permit.plan_hash != artifact.plan_hash
            or permit.authorization_scope_hash != artifact.authorization_scope_hash
            or permit.rollback_scope_hash != artifact.rollback_scope_hash
            or permit.patch_bytes_hash != artifact.patch_bytes_hash
            or permit.direction != artifact.direction
        ):
            raise MutationDisabledError(
                "permit is not bound to the exact persisted patch"
            )
        checked = permit.checked_at.astimezone(UTC)
        age = datetime.now(UTC) - checked
        if age < -timedelta(seconds=5) or age > MAX_PERMIT_AGE:
            raise MutationDisabledError("live mutation permit is stale")

    def apply_validated_patch(
        self,
        target: KubernetesTarget,
        artifact: ValidatedPatchArtifact,
        permit: LiveMutationPermit,
    ) -> MutationResult:
        self._validate(target, artifact, permit)
        body = json.loads(artifact.patch_bytes_utf8)
        try:
            response = self._apps_api.patch_namespaced_deployment(
                name=target.name,
                namespace=target.namespace,
                body=body,
                field_manager="agent4-deployment-recovery",
                field_validation="Strict",
                _request_timeout=self._request_timeout,
            )
        except Exception as exc:
            mapped = _map_api_error(exc, action="controlled Deployment mutation")
            if isinstance(
                mapped, (KubernetesTimeoutError, KubernetesConnectivityError)
            ):
                return MutationResult(
                    outcome=MutationOutcome.UNCERTAIN,
                    reason=str(mapped),
                )
            raise mapped from exc
        metadata = getattr(response, "metadata", None)
        resource_version = getattr(metadata, "resource_version", None)
        generation = getattr(metadata, "generation", None)
        if isinstance(response, dict):
            raw_metadata = response.get("metadata") or {}
            resource_version = raw_metadata.get("resourceVersion", resource_version)
            generation = raw_metadata.get("generation", generation)
        if resource_version is None or generation is None:
            raise KubernetesMalformedResponseError(
                "controlled mutation response omitted resourceVersion or generation"
            )
        return MutationResult(
            outcome=MutationOutcome.APPLIED,
            reason="Kubernetes accepted the exact persisted JSON Patch body",
            response_resource_version=(
                str(resource_version) if resource_version else None
            ),
            response_generation=(int(generation) if generation is not None else None),
        )
