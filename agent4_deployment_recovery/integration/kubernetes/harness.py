"""Operator-gated Kubernetes integration harness; disabled by default."""

import os
from pathlib import Path

from pydantic import model_validator

from agent4_deployment_recovery.adapters.kubernetes import OfficialKubernetesAdapter
from agent4_deployment_recovery.core.kubernetes_models import (
    KubernetesConfigMode,
    KubernetesConnectionConfig,
)
from agent4_deployment_recovery.core.schemas import (
    Identifier,
    KubernetesName,
    KubernetesTarget,
    NonEmptyStr,
    OpaqueKubernetesIdentity,
    StrictModel,
)

OPT_IN_ENV = "AGENT4_K8S_INTEGRATION"
OPT_IN_VALUE = "I_UNDERSTAND_DISPOSABLE_CLUSTER"


class KubernetesHarnessConfig(StrictModel):
    kubeconfig_path: Path
    context: NonEmptyStr
    cluster_id: Identifier
    namespace: KubernetesName
    deployment: KubernetesName
    container: KubernetesName
    target_uid: OpaqueKubernetesIdentity
    expected_resource_version: OpaqueKubernetesIdentity
    denied_namespace: KubernetesName | None = None

    @model_validator(mode="after")
    def enforce_disposable_scope(self):
        if not self.kubeconfig_path.is_absolute() or not self.kubeconfig_path.is_file():
            raise ValueError("integration kubeconfig must be an absolute existing file")
        if not self.namespace.startswith("agent4-it-"):
            raise ValueError("integration namespace must use the agent4-it- prefix")
        if self.deployment != "agent4-integration-target":
            raise ValueError("harness is restricted to agent4-integration-target")
        return self

    @classmethod
    def from_operator_environment(cls):
        if os.environ.get(OPT_IN_ENV) != OPT_IN_VALUE:
            raise PermissionError(
                f"set {OPT_IN_ENV}={OPT_IN_VALUE} only for a disposable test cluster"
            )
        names = {
            "kubeconfig_path": "AGENT4_K8S_KUBECONFIG",
            "context": "AGENT4_K8S_CONTEXT",
            "cluster_id": "AGENT4_K8S_CLUSTER_ID",
            "namespace": "AGENT4_K8S_NAMESPACE",
            "deployment": "AGENT4_K8S_DEPLOYMENT",
            "container": "AGENT4_K8S_CONTAINER",
            "target_uid": "AGENT4_K8S_TARGET_UID",
            "expected_resource_version": "AGENT4_K8S_RESOURCE_VERSION",
        }
        missing = [env_name for env_name in names.values() if not os.environ.get(env_name)]
        if missing:
            raise ValueError(f"integration configuration is incomplete: {', '.join(missing)}")
        payload = {field: os.environ[env_name] for field, env_name in names.items()}
        payload["denied_namespace"] = os.environ.get("AGENT4_K8S_DENIED_NAMESPACE")
        return cls.model_validate(payload)

    @property
    def target(self) -> KubernetesTarget:
        return KubernetesTarget(
            cluster_id=self.cluster_id,
            namespace=self.namespace,
            name=self.deployment,
            container=self.container,
            uid=self.target_uid,
            expected_resource_version=self.expected_resource_version,
        )

    def adapter(self) -> OfficialKubernetesAdapter:
        return OfficialKubernetesAdapter.from_config(
            KubernetesConnectionConfig(
                mode=KubernetesConfigMode.KUBECONFIG,
                cluster_id=self.cluster_id,
                kubeconfig_path=self.kubeconfig_path,
                context=self.context,
            )
        )
