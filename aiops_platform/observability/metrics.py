"""Observability of the multi-agent platform itself (v5 section 18).

Separate from application monitoring (Agent 1 watches the services; this
watches the agents and the workflow). Exposed as Prometheus metrics.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram


class PlatformMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.workflows = Counter("aiops_workflows_started_total", "Workflows started", ["kind"], registry=r)
        self.finished = Counter("aiops_workflows_finished_total", "Workflows finished", ["kind", "stage"], registry=r)
        self.duration = Histogram(
            "aiops_workflow_duration_seconds",
            "Workflow duration (detection to close)",
            ["kind"],
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=r,
        )
        self.policy = Counter(
            "aiops_policy_decisions_total", "AUTO / APPROVAL_REQUIRED / BLOCK", ["decision"], registry=r
        )
        self.hitl = Counter("aiops_hitl_resolutions_total", "Human decisions", ["state"], registry=r)
        self.task_latency = Histogram(
            "aiops_agent_task_seconds",
            "Agent task latency",
            ["agent", "status"],
            buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
            registry=r,
        )
        self.dead_letters = Counter(
            "aiops_dead_letters_total", "Dead-lettered messages", ["group", "topic", "code"], registry=r
        )
        self.duplicates = Counter(
            "aiops_duplicates_total", "Redelivered messages ignored", ["group", "topic"], registry=r
        )
        self.retries = Counter("aiops_retries_total", "Retried messages", ["group", "topic"], registry=r)

    def workflow_started(self, kind: str) -> None:
        self.workflows.labels(kind).inc()

    def workflow_finished(self, kind: str, stage: str, seconds: float) -> None:
        self.finished.labels(kind, stage).inc()
        self.duration.labels(kind).observe(seconds)

    def policy_decision(self, decision: str) -> None:
        self.policy.labels(decision).inc()

    def hitl_resolved(self, state: str, seconds: float) -> None:
        del seconds
        self.hitl.labels(state).inc()
