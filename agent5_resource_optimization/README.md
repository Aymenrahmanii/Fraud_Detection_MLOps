# Agent 5 - Resource Optimization

Agent 5 evaluates a sustained resource metric window and emits the
`RESOURCE_RECOMMENDATION` contract defined by the six-agent architecture.
It is deterministic and reliability-first: it recommends a bounded action but
does not mutate Docker or Kubernetes. The Orchestrator and Agent 4 own approval
and execution.

All recommendation files produced by the CLI are stored inside this Agent 5
directory. The `--out` option accepts a filename only, not a directory, so the
module remains portable and self-contained.

## Run against the local fraud API container

Run the CLI from a local Python environment because the collector calls the
host Docker CLI:

```powershell
python -m agent5_resource_optimization.cli `
  --docker-container fraud-api-base-check `
  --latency-p95-ms 250 `
  --rps 10 `
  --out resource_recommendation.json
```

The Docker collector is for local validation. In the complete architecture,
Agent 1 or the Monitoring Service supplies a sustained metric window to Agent 5.

For repeatable scenarios, provide a JSON metrics file instead:

```powershell
python -m agent5_resource_optimization.cli `
  --metrics-file agent5_resource_optimization/examples/high_load.json `
  --out resource_recommendation.json
```

## Real local metrics: Docker + Prometheus

Start the isolated Prometheus collector while the fraud API is available on
port 8000:

```cmd
docker run -d --name agent5-prometheus -p 9091:9090 -v "%cd%\agent5_resource_optimization\examples\prometheus-local.yml:/etc/prometheus/prometheus.yml:ro" prom/prometheus:latest
```

Then combine Docker infrastructure usage with Prometheus application SLIs:

```cmd
python -m agent5_resource_optimization.cli --docker-container fraud-api-base --prometheus-url http://localhost:9091 --window-minutes 5 --out agent5_real_metrics.json
```

Docker supplies CPU and memory. Prometheus supplies p95 latency, requests per
second and HTTP 5xx error rate. Queue depth remains null because this synchronous
API has no queue.

## Real Kubernetes metrics

This mode requires a configured kubectl context and Metrics Server:

```cmd
python -m agent5_resource_optimization.cli --k8s-deployment fraud-api --k8s-container fraud-api --namespace default --prometheus-url http://localhost:9090 --window-minutes 15 --out agent5_kubernetes_recommendation.json
```

The collector only performs kubectl reads. It reads desired/ready replicas and
resource requests/limits from the Deployment, then pod CPU/memory usage from
`metrics.k8s.io`. It never patches or scales the Deployment.

## MCP server

After installing the optional MCP dependency, start the server with:

```powershell
python -m agent5_resource_optimization.mcp_server
```

It exposes `create_resource_recommendation` and `get_latest_recommendation`.
