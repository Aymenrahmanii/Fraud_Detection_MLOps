from copy import deepcopy
from pathlib import Path

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from agent4_deployment_recovery.adapters.kubernetes import OfficialKubernetesAdapter
from agent4_deployment_recovery.core.dry_run import server_side_dry_run
from agent4_deployment_recovery.core.errors import (
    KubernetesConflictError,
    KubernetesConnectivityError,
    KubernetesForbiddenError,
    KubernetesMalformedResponseError,
    KubernetesNotFoundError,
    KubernetesTimeoutError,
    KubernetesUnsupportedError,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    ClusterIdentity,
    DryRunDecision,
    KubernetesConfigMode,
    KubernetesConnectionConfig,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.preflight import run_preflight


def _raw_deployment(*, duplicate_target=False):
    target = {
        "name": "fraud-api",
        "image": "registry.example/fraud-api@sha256:" + "b" * 64,
        "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
        "livenessProbe": {"httpGet": {"path": "/health", "port": 8000}},
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "200m", "memory": "256Mi"},
        },
    }
    containers = [
        {
            "name": "metrics-sidecar",
            "image": "registry.example/metrics@sha256:" + "c" * 64,
        },
        target,
    ]
    if duplicate_target:
        containers.append(deepcopy(target))
    return {
        "metadata": {
            "name": "fraud-api",
            "namespace": "default",
            "uid": "uid-fraud-api-001",
            "resourceVersion": "12345",
            "generation": 4,
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "fraud-api"}},
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
            },
            "template": {
                "metadata": {"labels": {"app": "fraud-api"}},
                "spec": {"containers": containers},
            },
        },
        "status": {
            "observedGeneration": 4,
            "replicas": 2,
            "readyReplicas": 2,
            "updatedReplicas": 2,
            "availableReplicas": 2,
            "conditions": [
                {
                    "type": "Available",
                    "status": "True",
                    "reason": "MinimumReplicasAvailable",
                }
            ],
        },
    }


class FakeAppsApi:
    def __init__(self, response=None, error=None):
        self.response = response or _raw_deployment()
        self.error = error
        self.read_calls = []
        self.patch_calls = []

    def read_namespaced_deployment(self, **kwargs):
        self.read_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response

    def patch_namespaced_deployment(self, **kwargs):
        self.patch_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _adapter(apps_api):
    return OfficialKubernetesAdapter(
        identity=ClusterIdentity(
            cluster_id="cluster-dev",
            mode=KubernetesConfigMode.KUBECONFIG,
            context="dev-context",
            api_server="https://kubernetes.example",
        ),
        request_timeout=(1.0, 2.0),
        apps_api=apps_api,
    )


def test_connection_configuration_never_falls_back_implicitly():
    with pytest.raises(ValidationError, match="explicit path and context"):
        KubernetesConnectionConfig(
            mode=KubernetesConfigMode.KUBECONFIG,
            cluster_id="cluster-dev",
            kubeconfig_path=Path("designated-config"),
        )
    with pytest.raises(ValidationError, match="cannot contain"):
        KubernetesConnectionConfig(
            mode=KubernetesConfigMode.IN_CLUSTER,
            cluster_id="cluster-dev",
            context="implicit-context",
        )


def test_exact_namespaced_read_captures_deployment_shape(operation_factory):
    api = FakeAppsApi()
    snapshot = _adapter(api).inspect_deployment(operation_factory().target)
    assert api.read_calls == [
        {
            "name": "fraud-api",
            "namespace": "default",
            "_request_timeout": (1.0, 2.0),
        }
    ]
    assert snapshot.uid == "uid-fraud-api-001"
    assert snapshot.resource_version == "12345"
    assert snapshot.generation == snapshot.observed_generation == 4
    assert snapshot.strategy_type == "RollingUpdate"
    assert snapshot.matching_containers("fraud-api")[0][0] == 1


def test_duplicate_target_and_malformed_responses_are_rejected(operation_factory):
    with pytest.raises(KubernetesMalformedResponseError, match="ambiguous"):
        _adapter(
            FakeAppsApi(_raw_deployment(duplicate_target=True))
        ).inspect_deployment(operation_factory().target)
    with pytest.raises(KubernetesMalformedResponseError, match="malformed"):
        _adapter(FakeAppsApi({"metadata": {}})).inspect_deployment(
            operation_factory().target
        )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ApiException(status=403, reason="forbidden"), KubernetesForbiddenError),
        (ApiException(status=404, reason="not found"), KubernetesNotFoundError),
        (TimeoutError("bounded timeout"), KubernetesTimeoutError),
        (ConnectionError("unreachable"), KubernetesConnectivityError),
    ],
)
def test_inspection_error_classes(operation_factory, error, expected):
    with pytest.raises(expected):
        _adapter(FakeAppsApi(error=error)).inspect_deployment(
            operation_factory().target
        )


def test_server_dry_run_always_sets_all_and_maps_conflict(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    preflight = run_preflight(operation, snapshot, preflight_policy)
    preview = build_typed_patch(operation, snapshot, preflight)
    api = FakeAppsApi()
    _adapter(api).server_side_dry_run(operation.target, preview)
    assert len(api.patch_calls) == 1
    assert api.patch_calls[0]["dry_run"] == "All"
    assert api.patch_calls[0]["field_validation"] == "Strict"
    assert api.patch_calls[0]["body"] == [
        item.model_dump(mode="json") for item in preview.patch
    ]

    with pytest.raises(KubernetesConflictError):
        _adapter(
            FakeAppsApi(error=ApiException(status=409, reason="test operation failed"))
        ).server_side_dry_run(operation.target, preview)
    with pytest.raises(KubernetesUnsupportedError):
        _adapter(
            FakeAppsApi(error=TypeError("dry_run is unsupported"))
        ).server_side_dry_run(operation.target, preview)
    assert not hasattr(_adapter(api), "apply")
    assert not hasattr(_adapter(api), "execute")


class RecordingDryRunAdapter:
    def __init__(self, response, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    @property
    def cluster_identity(self):
        return self.response.cluster

    def inspect_deployment(self, target):
        del target
        return self.response

    def server_side_dry_run(self, target, preview):
        del target, preview
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def test_dry_run_requires_opt_in_and_never_claims_rollout_success(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    preview = build_typed_patch(
        operation,
        snapshot,
        run_preflight(operation, snapshot, preflight_policy),
    )
    adapter = RecordingDryRunAdapter(snapshot)
    blocked = server_side_dry_run(adapter, operation, preview)
    assert blocked.decision == DryRunDecision.BLOCK
    assert adapter.calls == 0

    accepted = server_side_dry_run(adapter, operation, preview, contact_cluster=True)
    assert accepted.admission_accepted is True
    assert accepted.persisted is False
    assert accepted.rollout_verified is False
    assert adapter.calls == 1


def test_dry_run_failure_is_structured_block(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    preview = build_typed_patch(
        operation,
        snapshot,
        run_preflight(operation, snapshot, preflight_policy),
    )
    adapter = RecordingDryRunAdapter(
        snapshot, KubernetesForbiddenError("dry-run forbidden")
    )
    result = server_side_dry_run(adapter, operation, preview, contact_cluster=True)
    assert result.decision == DryRunDecision.BLOCK
    assert result.admission_accepted is False
    assert "forbidden" in result.reason
