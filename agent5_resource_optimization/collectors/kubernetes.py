"""Read Kubernetes desired state and pod usage without mutating the cluster."""

import json
import re
import subprocess
from urllib.parse import quote

from agent5_resource_optimization.core.schemas import ResourceMetrics


_MEMORY_TO_MIB = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024**2}
_CPU_TO_CORES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1}


def _quantity(value: str, units: dict[str, float]) -> float:
    match = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", value)
    if not match or match.group(2) not in units:
        raise ValueError(f"unsupported Kubernetes quantity: {value}")
    return float(match.group(1)) * units[match.group(2)]


def _kubectl_json(arguments: list[str]) -> dict:
    completed = subprocess.run(
        ["kubectl", *arguments], capture_output=True, text=True, check=False, timeout=30
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "kubectl command failed")
    return json.loads(completed.stdout)


def collect_kubernetes_metrics(
    deployment: str,
    *,
    namespace: str = "default",
    container: str | None = None,
    label_selector: str | None = None,
) -> ResourceMetrics:
    """Combine Deployment state/limits with Metrics API pod usage."""
    state = _kubectl_json(["get", "deployment", deployment, "-n", namespace, "-o", "json"])
    spec = state["spec"]
    status = state.get("status", {})
    containers = spec["template"]["spec"]["containers"]
    selected = next((item for item in containers if not container or item["name"] == container), None)
    if selected is None:
        raise RuntimeError(f"container {container!r} not found in deployment {deployment}")

    resources = selected.get("resources", {})
    limits = resources.get("limits", {})
    requests = resources.get("requests", {})
    if "cpu" not in limits or "memory" not in limits:
        raise RuntimeError("Kubernetes CPU and memory limits are required for utilization percentages")
    cpu_limit_cores = _quantity(limits["cpu"], _CPU_TO_CORES)
    memory_limit_mib = _quantity(limits["memory"], _MEMORY_TO_MIB)
    cpu_request_cores = _quantity(requests["cpu"], _CPU_TO_CORES) if "cpu" in requests else None
    memory_request_mib = (
        _quantity(requests["memory"], _MEMORY_TO_MIB) if "memory" in requests else None
    )

    selector = label_selector
    if selector is None:
        labels = spec.get("selector", {}).get("matchLabels", {})
        selector = ",".join(f"{key}={value}" for key, value in labels.items())
    raw_path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"
    if selector:
        raw_path += f"?labelSelector={quote(selector, safe='')}"
    usage = _kubectl_json(["get", "--raw", raw_path])

    cpu_values: list[float] = []
    memory_values: list[float] = []
    for pod in usage.get("items", []):
        pod_container = next(
            (item for item in pod.get("containers", []) if item["name"] == selected["name"]), None
        )
        if pod_container:
            cpu_values.append(_quantity(pod_container["usage"]["cpu"], _CPU_TO_CORES))
            memory_values.append(_quantity(pod_container["usage"]["memory"], _MEMORY_TO_MIB))
    if not cpu_values:
        raise RuntimeError(
            "Kubernetes Metrics API returned no matching pod usage; install Metrics Server and verify labels"
        )

    avg_cpu = sum(cpu_values) / len(cpu_values)
    avg_memory = sum(memory_values) / len(memory_values)
    return ResourceMetrics(
        replicas=spec.get("replicas", 1),
        ready_replicas=status.get("readyReplicas", 0),
        cpu_avg_pct=round(avg_cpu / cpu_limit_cores * 100, 2),
        memory_avg_pct=round(avg_memory / memory_limit_mib * 100, 2),
        cpu_usage_millicores=round(avg_cpu * 1000, 3),
        cpu_request_millicores=(
            round(cpu_request_cores * 1000, 3) if cpu_request_cores is not None else None
        ),
        cpu_limit_millicores=round(cpu_limit_cores * 1000, 3),
        memory_usage_mib=round(avg_memory, 2),
        memory_request_mib=(round(memory_request_mib, 2) if memory_request_mib is not None else None),
        memory_limit_mib=round(memory_limit_mib, 2),
    )
