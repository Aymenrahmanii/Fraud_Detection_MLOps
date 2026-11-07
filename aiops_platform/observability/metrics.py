"""Observability of the multi-agent platform itself (v5 section 18).

Separate from application monitoring (Agent 1 watches the services; this
watches the agents and the workflow). Every process owns one
`PlatformMetrics` and exposes it as Prometheus text: the Orchestrator on its
HTTP API (`/metrics`), the Dashboard on `/metrics`, every other process on
`METRICS_PORT` (default 9100).

v5 section 18 table -> metric:

| v5 metric                                 | Prometheus series                                       |
|-------------------------------------------|---------------------------------------------------------|
| Agent task latency and success rate       | aiops_agent_task_seconds{agent,objective,status}        |
| Kafka consumer lag                        | aiops_kafka_consumer_lag{group,topic,partition}         |
| gRPC error / timeout rate                 | aiops_context_rpc_seconds{method,code}                  |
| Context read/write latency                | aiops_context_rpc_seconds{method,code}                  |
| HITL waiting time / expiry rate           | aiops_hitl_wait_seconds{state}, aiops_hitl_resolutions  |
| Policy AUTO/APPROVAL/BLOCK distribution    | aiops_policy_decisions_total{decision}                  |
| False action / rollback rate              | aiops_actions_total{type,outcome}                       |
| Message reliability                       | aiops_messages_processed/retries/dead_letters/duplicates|
| End-to-end (MTTR) by workflow kind        | aiops_workflow_duration_seconds{kind}                   |
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from aiops_platform.bus.reliability import ConsumerObserver

FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SLOW = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)


class PlatformMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.workflows = Counter("aiops_workflows_started_total", "Workflows started", ["kind"], registry=r)
        self.finished = Counter("aiops_workflows_finished_total", "Workflows finished", ["kind", "stage"], registry=r)
        self.duration = Histogram(
            "aiops_workflow_duration_seconds",
            "Workflow duration, opened to closed (MTTR for incidents)",
            ["kind"],
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=r,
        )
        self.policy = Counter("aiops_policy_decisions_total", "Policy gate decisions", ["decision"], registry=r)
        self.hitl = Counter("aiops_hitl_resolutions_total", "Human decisions and expiries", ["state"], registry=r)
        self.hitl_wait = Histogram(
            "aiops_hitl_wait_seconds", "Approval request to human decision", ["state"], buckets=SLOW, registry=r
        )
        self.actions = Counter(
            "aiops_actions_total", "Executed actions by verified outcome", ["type", "outcome"], registry=r
        )
        self.task_latency = Histogram(
            "aiops_agent_task_seconds",
            "Agent task latency (worker receives task -> result published)",
            ["agent", "objective", "status"],
            buckets=SLOW,
            registry=r,
        )
        self.processing = Histogram(
            "aiops_message_processing_seconds",
            "Handler time per consumed event",
            ["group", "topic"],
            buckets=FAST,
            registry=r,
        )
        self.processed = Counter("aiops_messages_processed_total", "Events processed", ["group", "topic"], registry=r)
        self.dead_letters = Counter(
            "aiops_dead_letters_total", "Dead-lettered messages", ["group", "topic", "code"], registry=r
        )
        self.duplicates = Counter(
            "aiops_duplicates_total", "Redelivered messages ignored", ["group", "topic"], registry=r
        )
        self.retries = Counter("aiops_retries_total", "Retried messages", ["group", "topic"], registry=r)
        self.context_rpc = Histogram(
            "aiops_context_rpc_seconds",
            "Context Service call latency by result code",
            ["method", "code"],
            buckets=FAST,
            registry=r,
        )
        self.unrouted = Counter(
            "aiops_unrouted_events_total", "Events for a workflow that was not waiting", ["topic"], registry=r
        )
        self.consumer_lag = Gauge(
            "aiops_kafka_consumer_lag", "Records behind the partition end", ["group", "topic", "partition"], registry=r
        )

    def observer(self) -> ConsumerObserver:
        return _BusObserver(self)

    def workflow_started(self, kind: str) -> None:
        self.workflows.labels(kind).inc()

    def workflow_finished(self, kind: str, stage: str, seconds: float) -> None:
        self.finished.labels(kind, stage).inc()
        self.duration.labels(kind).observe(seconds)

    def policy_decision(self, decision: str) -> None:
        self.policy.labels(decision).inc()

    def hitl_resolved(self, state: str, wait_seconds: float | None) -> None:
        self.hitl.labels(state).inc()
        if wait_seconds is not None and wait_seconds >= 0:
            self.hitl_wait.labels(state).observe(wait_seconds)

    def action_outcome(self, action_type: str, outcome: str) -> None:
        self.actions.labels(action_type, outcome).inc()

    def task_done(self, agent: str, objective: str, status: str, seconds: float) -> None:
        self.task_latency.labels(agent, objective, status).observe(seconds)


class _BusObserver(ConsumerObserver):
    def __init__(self, metrics: PlatformMetrics) -> None:
        self._m = metrics

    def processed(self, group: str, topic: str, seconds: float) -> None:
        self._m.processed.labels(group, topic).inc()
        self._m.processing.labels(group, topic).observe(seconds)

    def duplicate(self, group: str, topic: str) -> None:
        self._m.duplicates.labels(group, topic).inc()

    def retried(self, group: str, topic: str, attempt: int) -> None:
        del attempt
        self._m.retries.labels(group, topic).inc()

    def dead_lettered(self, group: str, topic: str, code: str) -> None:
        self._m.dead_letters.labels(group, topic, code).inc()


def serve_metrics(metrics: PlatformMetrics, port: int) -> None:
    """Expose a process's metrics on http://0.0.0.0:<port>/metrics."""
    from prometheus_client import start_http_server

    start_http_server(port, registry=metrics.registry)
