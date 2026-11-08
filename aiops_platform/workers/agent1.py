"""Agent 1 (Monitoring & Anomaly Detection) worker.

Agent 1's own `MonitoringAgent` handles the Orchestrator's `detect_anomaly` and
`verify_recovery` tasks; its events are captured from its `EventPublisher` port
and re-published on the bus with the task's causation.

Two telemetry modes:

* live: Agent 1's full detector stack on Prometheus/Loki (`live.py`);
* simulated: `SimAnomalyDetector` and `SimSignalReader` read the simulated
  cluster. Detection thresholds are simple SLO rules here, but recovery is
  judged by Agent 1's real `recovery.Verifier` (sampling, PASS/FAIL/UNKNOWN,
  RECOVERED/NOT_RECOVERED/INCONCLUSIVE).

`Agent1VerdictService` answers Agent 4's bound verification requests with a
verdict signed by Agent 1's key, so Agent 4 can finish an execution.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import JsonValue

from agent1_monitoring.recovery import RecoveryPolicy, Verifier
from agent1_monitoring.service import MonitoringAgent
from agent1_monitoring.shared_contracts.enums import Environment as A1Environment
from agent1_monitoring.shared_contracts.enums import Severity as A1Severity
from agent1_monitoring.shared_contracts.events import EventEnvelope as A1Envelope
from agent1_monitoring.shared_contracts.payloads import AnomalyDetected as A1Anomaly
from agent1_monitoring.shared_contracts.tasks import AgentTask as A1Task
from agent1_monitoring.shared_contracts.tasks import TaskContext as A1Context
from agent4_deployment_recovery.core.integration_models import (
    RecoveryVerificationClaimsV1,
    RecoveryVerificationRequestV1,
    SignedRecoveryVerificationEnvelopeV1,
)
from aiops_platform.bus import publish
from aiops_platform.contracts.enums import AgentId, Environment, Priority, Severity
from aiops_platform.contracts.events import EventEnvelope, ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.hitl.keys import AGENT4_AUDIENCE, SigningKey
from aiops_platform.workers.base import AgentWorker, WorkerOutput

log = logging.getLogger(__name__)
AGENT1_ISSUER = "agent1-monitoring"

# SLO-style thresholds of the simulated detector (category -> (signal, limit)).
SIM_RULES: dict[str, tuple[str, float]] = {
    "memory_pressure": ("memory_pct", 85.0),
    "error_rate": ("error_rate", 0.02),
    "latency": ("latency_p95_ms", 500.0),
    "cpu_pressure": ("cpu_avg_pct", 85.0),
}


class SimSignalReader:
    """Agent 1 `SignalReader` over the simulated cluster."""

    def __init__(self, cluster) -> None:
        self._cluster = cluster

    async def read(self, service_id: str) -> dict[str, float | None]:
        s = self._cluster.signals(service_id)
        return {
            "error_rate": s["error_rate"],
            "latency_p95_ms": s["latency_p95_ms"],
            "memory_slope_mb_min": s["memory_slope_mb_min"],
        }


class SimAnomalyDetector:
    """Agent 1 `AnomalyDetector` port over simulated telemetry."""

    def __init__(self, cluster) -> None:
        self._cluster = cluster
        self.last_severity = A1Severity.MEDIUM

    async def detect(self, context: A1Context) -> A1Anomaly | None:
        s = self._cluster.signals(context.service_id, context.namespace)
        categories = [c for c, (signal, limit) in SIM_RULES.items() if s[signal] > limit]
        # Like Agent 1's statistical "growth" rule: a steady climb is memory pressure
        # before the absolute threshold is reached.
        if "memory_pressure" not in categories and s["memory_slope_mb_min"] > 5 and s["memory_pct"] > 75:
            categories.append("memory_pressure")
        if s["pod_restarts"] > 0:
            categories.append("memory_oom")
        if not categories:
            return None
        severe = {"memory_oom", "error_rate"} & set(categories) or s["latency_p95_ms"] > 1000
        self.last_severity = A1Severity.HIGH if severe else A1Severity.MEDIUM
        score = min(0.99, 0.55 + 0.12 * len(categories))
        return A1Anomaly(
            anomaly_id=f"ANM-{context.service_id}-{'+'.join(sorted(categories))}",
            detector="sim-slo+oom",
            anomaly_score=round(score, 4),
            signals=self._agent1_signals(context, sorted(categories), s),
        )

    def _agent1_signals(self, context: A1Context, categories: list[str], s: dict[str, float]) -> dict:
        """The same shape as Agent 1's real `build_anomaly_event` signals (Agent 2 reads it)."""
        now = datetime.now(UTC)
        # A leak becomes measurable a few minutes after its release; other symptoms just now.
        since_deploy = s["minutes_since_deployment"]
        growth_started = now - timedelta(minutes=max(0.0, since_deploy - 6.0))
        incidents = []
        for category in categories:
            first_seen = growth_started if category in ("memory_pressure", "memory_oom") else now - timedelta(minutes=2)
            facts: dict[str, JsonValue] = {}
            if category == "memory_pressure":
                facts = {
                    "statistical.signal": "memory_mb",
                    "statistical.method": "growth",
                    "statistical.current": round(s["memory_pct"] * s["memory_limit_mib"] / 100, 1),
                    "statistical.baseline_median": round(0.61 * s["memory_limit_mib"], 1),
                    "statistical.slope_per_minute": s["memory_slope_mb_min"],
                }
            elif category == "memory_oom":
                facts = {
                    "events.oom_kills": s["pod_restarts"],
                    "events.restarts": s["pod_restarts"],
                    "events.memory_peak_ratio": min(1.0, s["memory_pct"] / 100),
                }
            incidents.append(
                {
                    "incident_id": f"SIM-{context.service_id}-{category}",
                    "service_id": context.service_id,
                    "category": category,
                    "severity": self.last_severity.value,
                    "sources": ["sim-detector"],
                    "occurrences": 1,
                    "first_seen": first_seen.isoformat(),
                    "last_seen": now.isoformat(),
                    "facts": facts,
                }
            )
        return {
            "categories": categories,
            "observed_on": [context.service_id],
            "incidents": incidents,
            "suspects": [{"service_id": context.service_id, "probability": 0.9}],
            "decision_reason": "simulated SLO/growth rules: " + ", ".join(categories),
            # Direct readings Agent 2 understands.
            "memory_pct": s["memory_pct"],
            "latency_p95_ms": s["latency_p95_ms"],
            "error_rate": s["error_rate"],
            "pod_restarts": s["pod_restarts"],
            "cpu_percent": s["cpu_avg_pct"],
        }


class CapturingPublisher:
    """Agent 1 `EventPublisher` that keeps what Agent 1 wanted to publish."""

    def __init__(self) -> None:
        self.events: list[A1Envelope] = []

    async def publish(self, event: A1Envelope) -> None:
        self.events.append(event)


def fast_recovery_policy(window_seconds: float = 60, sample_seconds: float = 30) -> RecoveryPolicy:
    return RecoveryPolicy(settle_seconds=0, window_seconds=window_seconds, sample_seconds=sample_seconds)


class Agent1VerdictService:
    """Signed recovery verdicts for Agent 4's bound verification requests."""

    def __init__(self, verifier: Verifier, signer: SigningKey) -> None:
        self._verifier = verifier
        self._signer = signer

    async def verify_execution(
        self, request: RecoveryVerificationRequestV1, service: dict[str, str]
    ) -> SignedRecoveryVerificationEnvelopeV1:
        started = datetime.now(UTC)
        if started < request.requested_at:
            started = request.requested_at
        result = await self._verifier.verify(
            request.execution_id,
            A1Context(
                service_id=service["service_id"],
                namespace=service["namespace"],
                environment=A1Environment(service.get("environment", "production")),
                signals={"categories": ["error_rate", "latency", "memory_pressure"]},
            ),
        )
        ended = max(datetime.now(UTC), started)
        verdict = {"RECOVERED": "VERIFIED", "NOT_RECOVERED": "FAILED"}.get(result.status, "INCONCLUSIVE")
        claims = RecoveryVerificationClaimsV1.model_validate(
            {
                "verdict_id": result.verification_id,
                "issuer": self._signer.issuer,
                "audience": AGENT4_AUDIENCE,
                "issued_at": ended,
                "expires_at": ended + timedelta(minutes=10),
                "request_id": request.request_id,
                "verification_requested_at": request.requested_at,
                "observation_started_at": started,
                "observation_ended_at": ended,
                "execution_id": request.execution_id,
                "correlation_id": request.correlation_id,
                "target_uid": request.target_uid,
                "expected_plan_hash": request.expected_plan_hash,
                "expected_patch_hash": request.expected_patch_hash,
                "expected_image": request.expected_image,
                "expected_version": request.expected_version,
                "verdict": verdict,
                "evidence_refs": (result.verification_id,),
                "detail": f"Agent 1 {result.status} over {result.window}: "
                + ", ".join(f"{k}={v}" for k, v in sorted(result.checks.items())),
            }
        )
        return SignedRecoveryVerificationEnvelopeV1(
            key_id=self._signer.key_id, claims=claims, signature=self._signer.sign(claims)
        )

    def sync_provider(
        self,
    ) -> Callable[[RecoveryVerificationRequestV1, dict[str, str]], SignedRecoveryVerificationEnvelopeV1]:
        """Agent 4's coordinator is synchronous and runs in a thread: run the check there."""

        def call(request: RecoveryVerificationRequestV1, service: dict[str, str]):
            return asyncio.run(self.verify_execution(request, service))

        return call


class MonitoringWorker(AgentWorker):
    agent_id = AgentId.MONITORING

    def __init__(self, bus, context, *, agent: MonitoringAgent, publisher: CapturingPublisher, **kwargs) -> None:
        super().__init__(bus, context, **kwargs)
        self._agent = agent
        self._captured = publisher

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        start = len(self._captured.events)
        result = await self._agent.handle(A1Task.model_validate(task.model_dump(mode="json")))
        events = []
        for event in self._captured.events[start:]:
            events.append((event.topic, dict(event.payload), event.confidence, tuple(result.evidence_refs)))
        platform_result = AgentResult.model_validate(
            {**result.model_dump(mode="json"), "payload": events[0][1] if events else {}}
        )
        return WorkerOutput(result=platform_result, events=events)


class SimWatcher:
    """Agent 1's autonomous watch loop over simulated services.

    Each `tick()` runs Agent 1's one-shot detection on every watched service and
    publishes a new `ANOMALY_DETECTED` (opening an incident) unless that service
    already has an open alert. `resolve()` re-arms a service once its incident closes.
    """

    def __init__(
        self, bus, cluster, detector: SimAnomalyDetector, services: list[ServiceIdentity], environment: Environment
    ) -> None:
        self._bus = bus
        self._cluster = cluster
        self._detector = detector
        self._services = services
        self._environment = environment
        self._open: dict[str, str] = {}
        self._counter = 0

    def resolve(self, service_id: str) -> None:
        self._open.pop(service_id, None)

    async def tick(self) -> list[str]:
        opened = []
        for service in self._services:
            anomaly = await self._detector.detect(
                A1Context(
                    service_id=service.service_id,
                    namespace=service.namespace,
                    environment=A1Environment(self._environment.value),
                )
            )
            if anomaly is None:
                # Healthy again (e.g. after a verified rollback): re-arm the service.
                self._open.pop(service.service_id, None)
                continue
            if service.service_id in self._open:
                continue  # the same alert is still open
            self._counter += 1
            # Unique across restarts: a counter alone restarts at 1 and would reuse the ID
            # of an already-closed incident, so the new outage would be ignored.
            incident_id = f"INC-{datetime.now(UTC):%Y%m%d%H%M%S}-{service.service_id}-{uuid4().hex[:6]}"
            dep = self._cluster.deployment(service.service_id, service.namespace)
            severity = Severity(self._detector.last_severity.value)
            signals: dict[str, JsonValue] = dict(anomaly.signals)
            envelope = EventEnvelope(
                correlation_id=incident_id,
                trace_id=f"TRACE-{incident_id}",
                source=AgentId.MONITORING.value,
                target="orchestrator",
                topic="monitoring.anomaly.detected",
                message_type="ANOMALY_DETECTED",
                priority={Severity.CRITICAL: Priority.P0, Severity.HIGH: Priority.P1}.get(severity, Priority.P2),
                severity=severity,
                environment=self._environment,
                service=ServiceIdentity(
                    service_id=service.service_id, namespace=service.namespace, version=dep.version
                ),
                confidence=anomaly.anomaly_score,
                idempotency_key=f"monitoring-agent:anomaly:{incident_id}",
                payload=A1Anomaly(
                    anomaly_id=anomaly.anomaly_id,
                    detector=anomaly.detector,
                    anomaly_score=anomaly.anomaly_score,
                    signals=signals,
                ).model_dump(mode="json"),
            )
            self._open[service.service_id] = incident_id
            await publish(self._bus, envelope)
            opened.append(incident_id)
        return opened


CPU_RECOVERY_LIMIT = SIM_RULES["cpu_pressure"][1]
CPU_COST_PER_REQUEST_BUDGET = 1.0  # hand-picked: twice the healthy simulated cost of 0.5


class ReleaseHealthVerifier:
    """Agent 1's recovery verifier, plus the check its symptom rules miss: CPU is back under the limit
    the detector uses. A release that burns CPU passes the error, latency and memory checks, so without
    this a scale-up that hides the bug is reported as recovered. Agent 1's own verifier is unchanged."""

    def __init__(self, inner: Verifier, cluster) -> None:
        self._inner = inner
        self._cluster = cluster

    async def verify(self, action_id: str, context):
        result = await self._inner.verify(action_id, context)
        signals = self._cluster.signals(context.service_id, getattr(context, "namespace", None))
        # Average CPU alone can hide a burn: spreading it over more pods lowers the average. CPU per request
        # (CPU points times pods, per request per second) does not: a healthy service costs about 0.5 here,
        # and CPU_COST_PER_REQUEST_BUDGET allows twice that.
        cost = signals["cpu_avg_pct"] * signals["replicas"] / signals["rps"] if signals["rps"] > 0 else 0.0
        checks = {
            "cpu_avg_pct": "PASS" if signals["cpu_avg_pct"] <= CPU_RECOVERY_LIMIT else "FAIL",
            "cpu_cost_per_request": "PASS" if cost <= CPU_COST_PER_REQUEST_BUDGET else "FAIL",
        }
        status = result.status
        if result.status == "RECOVERED" and "FAIL" in checks.values():
            status = "NOT_RECOVERED"
        return result.model_copy(update={"status": status, "checks": {**result.checks, **checks}})


def build_sim_monitoring(cluster, *, window_seconds: float = 60, sample_seconds: float = 30, sleep=None):
    """Agent 1 wired on the simulated cluster: (agent, publisher, detector, verifier)."""

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    verifier = ReleaseHealthVerifier(
        Verifier(
            SimSignalReader(cluster),
            fast_recovery_policy(window_seconds, sample_seconds),
            sleep=sleep or instant,
        ),
        cluster,
    )
    detector = SimAnomalyDetector(cluster)
    publisher = CapturingPublisher()
    agent = MonitoringAgent(detector=detector, verifier=verifier, publisher=publisher)
    return agent, publisher, detector, verifier
