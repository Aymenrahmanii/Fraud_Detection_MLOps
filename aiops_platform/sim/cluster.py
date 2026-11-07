"""A simulated Kubernetes cluster for the offline demo and the end-to-end tests.

It models what the closed loop needs and nothing more:

* Deployments with an image, replicas, a memory limit, a revision history and
  a uid/resourceVersion/generation that change on every write, exposed
  through Agent 4's own inspection and mutation interfaces, so Agent 4's real
  coordinator, preflight, patch builder and rollout observer run unchanged;
* releases that carry faults (a release can leak memory); rolling back to a
  release without the fault clears it;
* telemetry derived from release faults, load and replicas (CPU, latency,
  memory, error rate, restarts), read by Agent 1's verifier and the
  simulated detector.

Time is a simulated clock in minutes so a 10-minute observation window runs
instantly in tests.
"""

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent4_deployment_recovery.core.errors import KubernetesAdapterError
from agent4_deployment_recovery.core.execution_models import MutationOutcome, MutationResult
from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    ClusterIdentity,
    ContainerSnapshot,
    DeploymentSnapshot,
    KubernetesConfigMode,
    PreflightPolicy,
    ResourceRequirementsSnapshot,
)
from agent4_deployment_recovery.core.schemas import KubernetesTarget

CLUSTER_ID = "sim-cluster"
CAPACITY_RPS_PER_REPLICA = 120.0


@dataclass(frozen=True)
class Release:
    version: str
    image: str  # repository@sha256:<64 hex>
    faults: frozenset[str] = frozenset()  # e.g. {"memory_leak"}


def digest_image(repository: str, version: str) -> str:
    import hashlib

    return f"{repository}@sha256:{hashlib.sha256(f'{repository}:{version}'.encode()).hexdigest()}"


@dataclass
class SimDeployment:
    service_id: str
    namespace: str
    container: str
    releases: dict[str, Release]
    image: str
    replicas: int
    memory_limit_mib: int
    rps: float
    uid: str
    resource_version: int = 100
    generation: int = 1
    deployed_at_min: float = 0.0
    history: list[tuple[float, str]] = field(default_factory=list)  # (minute, version)
    restarts: int = 0

    @property
    def release(self) -> Release:
        for release in self.releases.values():
            if release.image == self.image:
                return release
        return Release(version="unknown", image=self.image)

    @property
    def version(self) -> str:
        return self.release.version


class SimulatedCluster:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._deployments: dict[tuple[str, str], SimDeployment] = {}
        self.minute = 0.0
        self.mutations: list[dict] = []

    # ---- scenario setup --------------------------------------------------

    def add_service(
        self,
        service_id: str,
        namespace: str,
        releases: list[Release],
        *,
        current: str,
        replicas: int = 4,
        memory_limit_mib: int = 512,
        rps: float = 300.0,
    ) -> SimDeployment:
        by_version = {r.version: r for r in releases}
        dep = SimDeployment(
            service_id=service_id,
            namespace=namespace,
            container=service_id,
            releases=by_version,
            image=by_version[current].image,
            replicas=replicas,
            memory_limit_mib=memory_limit_mib,
            rps=rps,
            uid=f"uid-{namespace}-{service_id}",
            history=[(self.minute, current)],
        )
        with self._lock:
            self._deployments[(namespace, service_id)] = dep
        return dep

    def release_version(self, service_id: str, namespace: str, version: str) -> None:
        """A deployment done outside the agents (e.g. by CI), with its own revision bump."""
        with self._lock:
            dep = self._get(namespace, service_id)
            dep.image = dep.releases[version].image
            dep.deployed_at_min = self.minute
            dep.history.append((self.minute, version))
            dep.restarts = 0
            self._bump(dep)

    def set_load(self, service_id: str, namespace: str, rps: float) -> None:
        with self._lock:
            self._get(namespace, service_id).rps = rps

    def advance(self, minutes: float) -> None:
        with self._lock:
            self.minute += minutes
            for dep in self._deployments.values():
                if self._memory_pct(dep) >= 95:
                    dep.restarts += 1  # OOMKilled once per simulated step at the limit

    def deployment(self, service_id: str, namespace: str) -> SimDeployment:
        with self._lock:
            return self._get(namespace, service_id)

    def deployments(self) -> list[SimDeployment]:
        with self._lock:
            return list(self._deployments.values())

    def previous_release(self, service_id: str, namespace: str) -> Release | None:
        """The last version before the current one with no known fault (rollback target)."""
        with self._lock:
            dep = self._get(namespace, service_id)
            for _, version in reversed(dep.history[:-1]):
                if version != dep.version:
                    return dep.releases[version]
        return None

    def preflight_policy(self) -> PreflightPolicy:
        with self._lock:
            return PreflightPolicy(
                allowed_targets=tuple(
                    AllowedDeploymentTarget(
                        cluster_id=CLUSTER_ID,
                        namespace=d.namespace,
                        workload=d.service_id,
                        container=d.container,
                    )
                    for d in self.deployments()
                )
            )

    # ---- telemetry -------------------------------------------------------

    def _minutes_on_release(self, dep: SimDeployment) -> float:
        return max(0.0, self.minute - dep.deployed_at_min)

    def _memory_pct(self, dep: SimDeployment) -> float:
        if "memory_leak" in dep.release.faults:
            leaked_mib = 12.1 * self._minutes_on_release(dep)
            return min(99.0, 60.0 + 100.0 * leaked_mib / dep.memory_limit_mib)
        return 61.0

    def signals(self, service_id: str, namespace: str | None = None) -> dict[str, float]:
        with self._lock:
            dep = (
                self._get(namespace, service_id)
                if namespace
                else next(d for d in self.deployments() if d.service_id == service_id)
            )
            cpu = min(99.0, 60.0 * dep.rps / (dep.replicas * CAPACITY_RPS_PER_REPLICA))
            memory = self._memory_pct(dep)
            leaking = "memory_leak" in dep.release.faults
            latency = 205.0 * (1 + max(0.0, (cpu - 70.0) / 10.0) ** 2 * 0.5)
            error_rate = 0.004
            if memory >= 90:
                latency += 900.0
                error_rate = 0.17
            elif cpu >= 90:
                error_rate = 0.03
            return {
                "cpu_avg_pct": round(cpu, 2),
                "memory_pct": round(memory, 2),
                "memory_slope_mb_min": 12.1 if leaking and memory < 99 else 0.2,
                "latency_p95_ms": round(latency, 1),
                "error_rate": error_rate,
                "pod_restarts": float(dep.restarts),
                "rps": dep.rps,
                "replicas": float(dep.replicas),
                "ready_replicas": float(dep.replicas),
                "memory_limit_mib": float(dep.memory_limit_mib),
                "minutes_since_deployment": round(self._minutes_on_release(dep), 2),
            }

    # ---- Agent 4 boundary -----------------------------------------------

    def target(self, service_id: str, namespace: str) -> KubernetesTarget:
        with self._lock:
            dep = self._get(namespace, service_id)
            return KubernetesTarget(
                cluster_id=CLUSTER_ID,
                namespace=namespace,
                name=service_id,
                container=dep.container,
                uid=dep.uid,
                expected_resource_version=str(dep.resource_version),
            )

    @property
    def cluster_identity(self) -> ClusterIdentity:
        return ClusterIdentity(
            cluster_id=CLUSTER_ID,
            mode=KubernetesConfigMode.KUBECONFIG,
            context="simulated",
            api_server="https://sim-cluster.invalid",
        )

    def snapshot(self, namespace: str, name: str) -> DeploymentSnapshot:
        with self._lock:
            dep = self._get(namespace, name)
            resources = {
                "requests": {"cpu": "250m", "memory": f"{dep.memory_limit_mib // 2}Mi"},
                "limits": {"cpu": "1000m", "memory": f"{dep.memory_limit_mib}Mi"},
            }
            probe = {"httpGet": {"path": "/health", "port": 8080}}
            raw = {
                "name": dep.container,
                "image": dep.image,
                "readinessProbe": probe,
                "livenessProbe": probe,
                "resources": resources,
            }
            container = ContainerSnapshot(
                name=dep.container,
                image=dep.image,
                readiness_probe=probe,
                liveness_probe=probe,
                resources=ResourceRequirementsSnapshot(**resources),
                resources_present=True,
                raw=raw,
            )
            return DeploymentSnapshot(
                cluster=self.cluster_identity,
                namespace=dep.namespace,
                name=dep.service_id,
                uid=dep.uid,
                resource_version=str(dep.resource_version),
                generation=dep.generation,
                observed_generation=dep.generation,
                replicas=dep.replicas,
                ready_replicas=dep.replicas,
                updated_replicas=dep.replicas,
                available_replicas=dep.replicas,
                selector={"matchLabels": {"app": dep.service_id}},
                pod_template={
                    "metadata": {"labels": {"app": dep.service_id}},
                    "spec": {"containers": [raw]},
                },
                containers=(container,),
                strategy_type="RollingUpdate",
                strategy={"type": "RollingUpdate"},
            )

    def apply_patch(self, namespace: str, name: str, patch) -> int:
        """Apply Agent 4's RFC 6902 patch atomically; returns the new generation."""
        with self._lock:
            dep = self._get(namespace, name)
            doc = self._document(dep)
            work = deepcopy(doc)
            for op in patch:
                parent, key = _resolve(work, op.path)
                if op.op == "test":
                    if parent[key] != op.value:
                        raise KubernetesAdapterError(f"patch test failed at {op.path}")
                elif op.op in ("replace", "add"):
                    parent[key] = op.value
                else:
                    raise KubernetesAdapterError(f"unsupported patch op {op.op}")
            container = work["spec"]["template"]["spec"]["containers"][0]
            changed_image = container["image"] != dep.image
            dep.image = container["image"]
            dep.replicas = work["spec"]["replicas"]
            limit = container.get("resources", {}).get("limits", {}).get("memory")
            if isinstance(limit, str) and limit.endswith("Mi"):
                dep.memory_limit_mib = int(limit[:-2])
            if changed_image:
                dep.deployed_at_min = self.minute
                dep.history.append((self.minute, dep.version))
                dep.restarts = 0
            self._bump(dep)
            self.mutations.append(
                {
                    "service": name,
                    "namespace": namespace,
                    "minute": self.minute,
                    "image": dep.image,
                    "replicas": dep.replicas,
                }
            )
            return dep.generation

    # ---- internals -------------------------------------------------------

    def _get(self, namespace: str, name: str) -> SimDeployment:
        try:
            return self._deployments[(namespace, name)]
        except KeyError as exc:
            raise KubernetesAdapterError(f"deployment {namespace}/{name} not found") from exc

    @staticmethod
    def _bump(dep: SimDeployment) -> None:
        dep.resource_version += 1
        dep.generation += 1

    def _document(self, dep: SimDeployment) -> dict:
        snap = self.snapshot(dep.namespace, dep.service_id)
        return {
            "metadata": {"uid": snap.uid, "resourceVersion": snap.resource_version},
            "spec": {
                "replicas": snap.replicas,
                "template": {"spec": {"containers": [deepcopy(snap.containers[0].raw)]}},
            },
        }


def _resolve(doc: dict, path: str):
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in path.lstrip("/").split("/")]
    node = doc
    for token in tokens[:-1]:
        node = node[int(token)] if isinstance(node, list) else node[token]
    last = tokens[-1]
    return node, (int(last) if isinstance(node, list) else last)


class SimInspector:
    """Agent 4 `KubernetesInspectionAdapter` over the simulated cluster."""

    def __init__(self, cluster: SimulatedCluster) -> None:
        self._cluster = cluster

    @property
    def cluster_identity(self) -> ClusterIdentity:
        return self._cluster.cluster_identity

    def inspect_deployment(self, target: KubernetesTarget) -> DeploymentSnapshot:
        if target.cluster_id != CLUSTER_ID:
            raise KubernetesAdapterError("target is not in the simulated cluster")
        return self._cluster.snapshot(target.namespace, target.name)

    def server_side_dry_run(self, target: KubernetesTarget, preview) -> DeploymentSnapshot:
        snapshot = self.inspect_deployment(target)
        if preview.base_resource_version != snapshot.resource_version:
            raise KubernetesAdapterError("dry-run conflict: resourceVersion changed")
        return snapshot


class SimMutationAdapter:
    """Agent 4 `KubernetesMutationAdapter`: applies only coordinator-validated artifacts."""

    def __init__(self, cluster: SimulatedCluster) -> None:
        self._cluster = cluster

    def apply_validated_patch(self, target: KubernetesTarget, artifact, permit) -> MutationResult:
        if permit.patch_bytes_hash != artifact.patch_bytes_hash:
            raise KubernetesAdapterError("permit does not bind this exact patch")
        generation = self._cluster.apply_patch(target.namespace, target.name, artifact.patch)
        return MutationResult(
            outcome=MutationOutcome.APPLIED,
            reason=f"simulated apply at {datetime.now(UTC).isoformat()}",
            response_generation=generation,
        )
