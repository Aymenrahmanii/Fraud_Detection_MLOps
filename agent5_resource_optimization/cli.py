"""Command-line entry point for Agent 5 resource recommendations."""

import argparse
import json
from pathlib import Path

from agent5_resource_optimization.collectors.docker import collect_docker_metrics
from agent5_resource_optimization.collectors.aggregate import enrich_with_prometheus
from agent5_resource_optimization.collectors.kubernetes import collect_kubernetes_metrics
from agent5_resource_optimization.core.optimizer import build_recommendation
from agent5_resource_optimization.core.schemas import OptimizationPolicy, ResourceMetrics, ServiceRef

_AGENT_DIR = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent 5 resource optimization")
    parser.add_argument("--metrics-file", type=Path, help="JSON file containing ResourceMetrics")
    parser.add_argument("--docker-container", help="Collect a local Docker stats snapshot")
    parser.add_argument("--k8s-deployment", help="Read a Kubernetes Deployment and pod Metrics API")
    parser.add_argument("--k8s-container", help="Container name inside the Kubernetes Deployment")
    parser.add_argument("--label-selector", help="Kubernetes pod label selector")
    parser.add_argument("--prometheus-url", help="Prometheus base URL, for example http://localhost:9090")
    parser.add_argument("--prometheus-handler", default="/predict")
    parser.add_argument("--latency-p95-ms", type=float)
    parser.add_argument("--rps", type=float)
    parser.add_argument("--error-rate-pct", type=float)
    parser.add_argument("--queue-depth", type=int)
    parser.add_argument("--service-id", default="fraud-api")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--version", default="unknown")
    parser.add_argument("--environment", default="development")
    parser.add_argument("--window-minutes", type=int, default=15)
    parser.add_argument("--min-replicas", type=int, default=1)
    parser.add_argument("--max-replicas", type=int, default=8)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("resource_recommendation.json"),
        help="Output filename stored inside agent5_resource_optimization",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    sources = [bool(args.metrics_file), bool(args.docker_container), bool(args.k8s_deployment)]
    if sum(sources) != 1:
        raise SystemExit("provide exactly one of --metrics-file, --docker-container or --k8s-deployment")

    if args.metrics_file:
        metrics = ResourceMetrics.model_validate_json(args.metrics_file.read_text(encoding="utf-8"))
    elif args.docker_container:
        metrics = collect_docker_metrics(
            args.docker_container,
            latency_p95_ms=args.latency_p95_ms,
            rps=args.rps,
            error_rate_pct=args.error_rate_pct,
            queue_depth=args.queue_depth,
        )
    else:
        metrics = collect_kubernetes_metrics(
            args.k8s_deployment,
            namespace=args.namespace,
            container=args.k8s_container,
            label_selector=args.label_selector,
        )

    if args.prometheus_url:
        metrics = enrich_with_prometheus(
            metrics,
            prometheus_url=args.prometheus_url,
            window_minutes=args.window_minutes,
            handler=args.prometheus_handler,
        )

    result = build_recommendation(
        metrics,
        policy=OptimizationPolicy(min_replicas=args.min_replicas, max_replicas=args.max_replicas),
        service=ServiceRef(service_id=args.service_id, namespace=args.namespace, version=args.version),
        environment=args.environment,
        window_minutes=args.window_minutes,
        evidence_refs=[
            reference
            for reference in (
                f"docker:{args.docker_container}" if args.docker_container else None,
                f"kubernetes:{args.namespace}/deployment/{args.k8s_deployment}"
                if args.k8s_deployment
                else None,
                f"prometheus:{args.prometheus_url}" if args.prometheus_url else None,
                str(args.metrics_file) if args.metrics_file else None,
            )
            if reference
        ],
    )
    payload = result.model_dump(mode="json", by_alias=True)
    if args.out.name != str(args.out):
        raise SystemExit("--out must be a filename without a directory")
    output_path = _AGENT_DIR / args.out.name
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"Recommendation saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
