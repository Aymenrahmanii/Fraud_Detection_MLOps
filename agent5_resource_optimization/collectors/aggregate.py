"""Merge infrastructure metrics with application metrics."""

from agent5_resource_optimization.collectors.prometheus import PrometheusCollector
from agent5_resource_optimization.core.schemas import ResourceMetrics


def enrich_with_prometheus(
    metrics: ResourceMetrics,
    *,
    prometheus_url: str,
    window_minutes: int,
    handler: str = "/predict",
) -> ResourceMetrics:
    application = PrometheusCollector(prometheus_url).collect(
        window_minutes=window_minutes, handler=handler
    )
    return metrics.model_copy(
        update={
            "latency_p95_ms": application.latency_p95_ms,
            "rps": application.rps,
            "error_rate_pct": application.error_rate_pct,
        }
    )
