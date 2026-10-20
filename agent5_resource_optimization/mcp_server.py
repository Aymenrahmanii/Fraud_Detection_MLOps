"""MCP interface for Agent 5 resource optimization."""

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from agent5_resource_optimization.core.optimizer import build_recommendation
from agent5_resource_optimization.core.schemas import OptimizationPolicy, ResourceMetrics, ServiceRef


server = MCPServer(
    "agent5-resource-optimization",
    instructions=(
        "Evaluates sustained CPU, memory, latency, traffic, queue and replica "
        "metrics. Returns a bounded RESOURCE_RECOMMENDATION for the "
        "Orchestrator. It never changes Docker or Kubernetes directly."
    ),
)

_CACHE_PATH = Path(__file__).resolve().parent / ".agent5_last_recommendation.json"


@server.tool()
def create_resource_recommendation(
    metrics: dict,
    service_id: str = "fraud-api",
    namespace: str = "default",
    environment: str = "development",
    min_replicas: int = 1,
    max_replicas: int = 8,
    window_minutes: int = 15,
) -> dict:
    """Create and cache a deterministic recommendation from a metric window."""
    result = build_recommendation(
        ResourceMetrics.model_validate(metrics),
        policy=OptimizationPolicy(min_replicas=min_replicas, max_replicas=max_replicas),
        service=ServiceRef(service_id=service_id, namespace=namespace),
        environment=environment,
        window_minutes=window_minutes,
    )
    payload = result.model_dump(mode="json", by_alias=True)
    _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


@server.tool()
def get_latest_recommendation() -> dict:
    """Return the latest cached recommendation without collecting new metrics."""
    if not _CACHE_PATH.exists():
        return {"error": "no recommendation has been created yet"}
    return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    server.run()
