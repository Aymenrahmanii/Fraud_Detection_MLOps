"""Official-client Kubernetes boundary for exact reads and server dry-run only."""

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NewConnectionError,
    ReadTimeoutError,
)

from agent4_deployment_recovery.core.errors import (
    KubernetesAdapterError,
    KubernetesConfigurationError,
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
    ContainerSnapshot,
    DeploymentSnapshot,
    KubernetesConfigMode,
    KubernetesConnectionConfig,
    ResourceRequirementsSnapshot,
    TypedPatchPreview,
)
from agent4_deployment_recovery.core.schemas import KubernetesTarget


@runtime_checkable
class KubernetesInspectionAdapter(Protocol):
    @property
    def cluster_identity(self) -> ClusterIdentity:
        """Return the explicitly selected cluster identity."""

    def inspect_deployment(self, target: KubernetesTarget) -> DeploymentSnapshot:
        """Read one exact namespaced Deployment."""

    def server_side_dry_run(
        self,
        target: KubernetesTarget,
        preview: TypedPatchPreview,
    ) -> DeploymentSnapshot:
        """Submit only the generated patch with dryRun=All."""


def _map_api_error(exc: Exception, *, action: str) -> KubernetesAdapterError:
    if isinstance(exc, ApiException):
        detail = str(exc.reason or exc.body or "Kubernetes API error")
        if exc.status == 403:
            return KubernetesForbiddenError(f"{action} forbidden: {detail}")
        if exc.status == 404:
            return KubernetesNotFoundError(f"{action} target not found: {detail}")
        if exc.status == 409:
            return KubernetesConflictError(f"{action} precondition conflict: {detail}")
        if exc.status in {408, 504}:
            return KubernetesTimeoutError(f"{action} timed out: {detail}")
        if exc.status in {0, None}:
            return KubernetesConnectivityError(f"{action} connection failed: {detail}")
        return KubernetesAdapterError(
            f"{action} rejected by API ({exc.status}): {detail}"
        )
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError, TimeoutError)):
        return KubernetesTimeoutError(f"{action} timed out: {exc}")
    if isinstance(exc, (MaxRetryError, NewConnectionError, ConnectionError)):
        return KubernetesConnectivityError(f"{action} connection failed: {exc}")
    return KubernetesAdapterError(f"{action} failed: {exc}")


class OfficialKubernetesAdapter:
    """Narrow official-client adapter with no live mutation method."""

    def __init__(
        self,
        *,
        identity: ClusterIdentity,
        request_timeout: tuple[float, float],
        apps_api: Any,
        api_client: client.ApiClient | None = None,
    ):
        self._identity = identity
        self._request_timeout = request_timeout
        self._apps_api = apps_api
        self._api_client = api_client

    @classmethod
    def from_config(cls, connection: KubernetesConnectionConfig):
        configuration = client.Configuration()
        context_name: str | None = None
        if connection.mode == KubernetesConfigMode.KUBECONFIG:
            path = Path(connection.kubeconfig_path).expanduser().resolve()
            if not path.is_file():
                raise KubernetesConfigurationError(
                    f"designated kubeconfig does not exist: {path}"
                )
            try:
                contexts, _ = config.list_kube_config_contexts(config_file=str(path))
            except Exception as exc:
                raise KubernetesConfigurationError(
                    f"unable to read designated kubeconfig: {exc}"
                ) from exc
            selected = next(
                (
                    item
                    for item in contexts or ()
                    if item.get("name") == connection.context
                ),
                None,
            )
            if selected is None:
                raise KubernetesConfigurationError(
                    f"designated context is absent: {connection.context}"
                )
            actual_cluster = selected.get("context", {}).get("cluster")
            if actual_cluster != connection.cluster_id:
                raise KubernetesConfigurationError(
                    "designated context cluster does not match configured cluster_id"
                )
            try:
                config.load_kube_config(
                    config_file=str(path),
                    context=connection.context,
                    client_configuration=configuration,
                )
            except Exception as exc:
                raise KubernetesConfigurationError(
                    f"unable to load designated kubeconfig context: {exc}"
                ) from exc
            context_name = connection.context
        elif connection.mode == KubernetesConfigMode.IN_CLUSTER:
            try:
                config.load_incluster_config(client_configuration=configuration)
            except Exception as exc:
                raise KubernetesConfigurationError(
                    f"explicit in-cluster configuration failed: {exc}"
                ) from exc
        else:
            raise KubernetesConfigurationError(
                "unsupported Kubernetes configuration mode"
            )

        api_client = client.ApiClient(configuration=configuration)
        identity = ClusterIdentity(
            cluster_id=connection.cluster_id,
            mode=connection.mode,
            context=context_name,
            api_server=configuration.host,
        )
        return cls(
            identity=identity,
            request_timeout=connection.request_timeout,
            apps_api=client.AppsV1Api(api_client),
            api_client=api_client,
        )

    @property
    def cluster_identity(self) -> ClusterIdentity:
        return self._identity

    def _validate_target_cluster(self, target: KubernetesTarget) -> None:
        if target.cluster_id != self._identity.cluster_id:
            raise KubernetesConfigurationError(
                "target cluster_id differs from the explicitly selected cluster"
            )

    def _serialized(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        api_client = self._api_client or client.ApiClient()
        serialized = api_client.sanitize_for_serialization(response)
        if not isinstance(serialized, dict):
            raise KubernetesMalformedResponseError(
                "Kubernetes Deployment response is not an object"
            )
        return serialized

    def _snapshot(
        self, response: Any, *, target: KubernetesTarget
    ) -> DeploymentSnapshot:
        raw = self._serialized(response)
        try:
            metadata = raw["metadata"]
            spec = raw["spec"]
            status = raw.get("status") or {}
            template = spec["template"]
            container_payloads = template["spec"]["containers"]
            strategy = spec.get("strategy") or {}
            containers = tuple(
                ContainerSnapshot(
                    name=item["name"],
                    image=item["image"],
                    readiness_probe=item.get("readinessProbe"),
                    liveness_probe=item.get("livenessProbe"),
                    resources=ResourceRequirementsSnapshot(
                        requests=dict(
                            (item.get("resources") or {}).get("requests") or {}
                        ),
                        limits=dict((item.get("resources") or {}).get("limits") or {}),
                    ),
                    resources_present="resources" in item,
                    raw=dict(item),
                )
                for item in container_payloads
            )
            payload = {
                "cluster": self._identity.model_dump(mode="json"),
                "namespace": metadata["namespace"],
                "name": metadata["name"],
                "uid": metadata["uid"],
                "resource_version": metadata["resourceVersion"],
                "generation": metadata["generation"],
                "observed_generation": status.get("observedGeneration", 0),
                "replicas": spec.get("replicas", 1),
                "ready_replicas": status.get("readyReplicas", 0),
                "updated_replicas": status.get("updatedReplicas", 0),
                "available_replicas": status.get("availableReplicas", 0),
                "selector": spec["selector"],
                "pod_template": template,
                "containers": [item.model_dump(mode="json") for item in containers],
                "strategy_type": strategy.get("type", "RollingUpdate"),
                "strategy": strategy,
                "conditions": status.get("conditions") or [],
            }
            snapshot = DeploymentSnapshot.model_validate_json(json.dumps(payload))
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise KubernetesMalformedResponseError(
                f"malformed Kubernetes Deployment response: {exc}"
            ) from exc
        matches = snapshot.matching_containers(target.container)
        if snapshot.namespace != target.namespace or snapshot.name != target.name:
            raise KubernetesMalformedResponseError(
                "Kubernetes returned a different namespaced Deployment than requested"
            )
        if len(matches) != 1:
            raise KubernetesMalformedResponseError(
                "target container is absent or ambiguous in Deployment response"
            )
        return snapshot

    def inspect_deployment(self, target: KubernetesTarget) -> DeploymentSnapshot:
        self._validate_target_cluster(target)
        try:
            response = self._apps_api.read_namespaced_deployment(
                name=target.name,
                namespace=target.namespace,
                _request_timeout=self._request_timeout,
            )
        except Exception as exc:
            raise _map_api_error(exc, action="Deployment inspection") from exc
        return self._snapshot(response, target=target)

    def server_side_dry_run(
        self,
        target: KubernetesTarget,
        preview: TypedPatchPreview,
    ) -> DeploymentSnapshot:
        self._validate_target_cluster(target)
        if preview.target_uid != target.uid:
            raise KubernetesConflictError("patch preview UID does not match target UID")
        try:
            response = self._apps_api.patch_namespaced_deployment(
                name=target.name,
                namespace=target.namespace,
                body=[item.model_dump(mode="json") for item in preview.patch],
                dry_run="All",
                field_manager="agent4-deployment-recovery",
                field_validation="Strict",
                _request_timeout=self._request_timeout,
            )
        except TypeError as exc:
            raise KubernetesUnsupportedError(
                f"API client does not support required dry-run arguments: {exc}"
            ) from exc
        except Exception as exc:
            raise _map_api_error(exc, action="server-side dry-run") from exc
        return self._snapshot(response, target=target)
