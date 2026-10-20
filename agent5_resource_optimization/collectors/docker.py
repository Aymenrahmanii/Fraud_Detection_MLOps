"""Collect a local development snapshot from Docker CLI."""

import json
import re
import subprocess

from agent5_resource_optimization.core.schemas import ResourceMetrics


_SIZE_UNITS = {"B": 1 / 1024**2, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}


def _mib(value: str) -> float:
    match = re.fullmatch(r"([0-9.]+)(B|KiB|MiB|GiB)", value.strip())
    if not match:
        raise ValueError(f"unsupported Docker memory value: {value}")
    return float(match.group(1)) * _SIZE_UNITS[match.group(2)]


def collect_docker_metrics(
    container: str,
    *,
    latency_p95_ms: float | None = None,
    rps: float | None = None,
    error_rate_pct: float | None = None,
    queue_depth: int | None = None,
) -> ResourceMetrics:
    """Read one Docker stats snapshot; intended for local validation only."""
    command = ["docker", "stats", "--no-stream", "--format", "{{json .}}", container]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"cannot inspect container {container}")
    if not completed.stdout.strip():
        raise RuntimeError(f"container {container} was not found or is not running")

    raw = json.loads(completed.stdout.splitlines()[0])
    usage_text, limit_text = (part.strip() for part in raw["MemUsage"].split("/", 1))
    return ResourceMetrics(
        replicas=1,
        ready_replicas=1,
        cpu_avg_pct=float(raw["CPUPerc"].rstrip("%")),
        memory_avg_pct=float(raw["MemPerc"].rstrip("%")),
        latency_p95_ms=latency_p95_ms,
        rps=rps,
        error_rate_pct=error_rate_pct,
        queue_depth=queue_depth,
        memory_usage_mib=round(_mib(usage_text), 2),
        memory_limit_mib=round(_mib(limit_text), 2),
    )
