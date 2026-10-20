"""Deterministic, reliability-first resource recommendation engine."""

import math
from uuid import uuid4

from agent5_resource_optimization.core.schemas import (
    ExpectedEffect,
    Forecast,
    OptimizationPolicy,
    RecommendationPayload,
    ResourceAction,
    ResourceMetrics,
    ResourceRecommendation,
    ServiceRef,
)


def _identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12].upper()}"


def _confidence(metrics: ResourceMetrics, window_minutes: int) -> float:
    optional = [metrics.latency_p95_ms, metrics.rps, metrics.error_rate_pct, metrics.queue_depth]
    completeness = sum(value is not None for value in optional) / len(optional)
    duration_score = min(window_minutes / 15, 1.0)
    readiness_score = metrics.ready_replicas / metrics.replicas
    return round(min(0.99, 0.55 + 0.2 * completeness + 0.15 * duration_score + 0.1 * readiness_score), 2)


def build_recommendation(
    metrics: ResourceMetrics,
    *,
    policy: OptimizationPolicy | None = None,
    service: ServiceRef | None = None,
    environment: str = "development",
    correlation_id: str | None = None,
    causation_id: str | None = None,
    trace_id: str | None = None,
    window_minutes: int = 15,
    evidence_refs: list[str] | None = None,
) -> ResourceRecommendation:
    """Evaluate one sustained metric window and return a bounded recommendation."""
    policy = policy or OptimizationPolicy()
    service = service or ServiceRef()
    correlation_id = correlation_id or _identifier("INC")
    trace_id = trace_id or _identifier("TRACE")
    recommendation_id = _identifier("RES")
    reasons: list[str] = []
    constraints = [
        f"replicas>={policy.min_replicas}",
        f"replicas<={policy.max_replicas}",
        "reliability_before_efficiency",
        "orchestrator_approval_required",
    ]

    latency_bad = metrics.latency_p95_ms is not None and metrics.latency_p95_ms > policy.latency_slo_ms
    queue_bad = metrics.queue_depth is not None and metrics.queue_depth > policy.high_queue_depth
    errors_bad = metrics.error_rate_pct is not None and metrics.error_rate_pct > policy.max_error_rate_pct
    cpu_high = metrics.cpu_avg_pct >= policy.scale_up_cpu_pct
    memory_high = metrics.memory_avg_pct >= policy.high_memory_pct

    action = ResourceAction(
        action="NO_CHANGE",
        **{"from": metrics.replicas, "to": metrics.replicas, "min": policy.min_replicas},
        max_policy=policy.max_replicas,
    )
    effect = ExpectedEffect(cpu_target_pct=metrics.cpu_avg_pct, memory_target_pct=metrics.memory_avg_pct)
    severity = "LOW"
    priority = "P3"
    rollback = "No change was proposed; continue monitoring the next observation window"

    if (cpu_high or latency_bad or queue_bad) and metrics.replicas < policy.max_replicas:
        target = min(policy.max_replicas, max(metrics.replicas + 1, math.ceil(metrics.replicas * policy.scale_up_factor)))
        action = ResourceAction(
            action="SCALE_REPLICAS",
            **{"from": metrics.replicas, "to": target, "min": policy.min_replicas},
            max_policy=policy.max_replicas,
        )
        if cpu_high:
            reasons.append(f"CPU {metrics.cpu_avg_pct:.1f}% exceeds {policy.scale_up_cpu_pct:.1f}%")
        if latency_bad:
            reasons.append(f"p95 latency {metrics.latency_p95_ms:.1f}ms exceeds {policy.latency_slo_ms:.1f}ms SLO")
        if queue_bad:
            reasons.append(f"queue depth {metrics.queue_depth} exceeds {policy.high_queue_depth}")
        ratio = metrics.replicas / target
        effect = ExpectedEffect(
            latency_reduction_pct=round(min(45, (1 - ratio) * 100), 1),
            cpu_target_pct=round(metrics.cpu_avg_pct * ratio, 1),
            memory_target_pct=metrics.memory_avg_pct,
        )
        severity, priority = ("HIGH", "P1") if latency_bad and cpu_high else ("MEDIUM", "P2")
        rollback = "Rollback if p95 latency does not improve by at least 15% after 5 minutes or error rate increases"
    elif memory_high and metrics.memory_limit_mib:
        new_limit = math.ceil(metrics.memory_limit_mib * policy.memory_increase_factor / 16) * 16
        action = ResourceAction(
            action="INCREASE_MEMORY",
            **{"from": metrics.replicas, "to": metrics.replicas, "min": policy.min_replicas},
            max_policy=policy.max_replicas,
            memory_limit_from_mib=metrics.memory_limit_mib,
            memory_limit_to_mib=new_limit,
        )
        reasons.append(f"memory {metrics.memory_avg_pct:.1f}% exceeds {policy.high_memory_pct:.1f}%")
        effect = ExpectedEffect(
            cpu_target_pct=metrics.cpu_avg_pct,
            memory_target_pct=round(metrics.memory_avg_pct * metrics.memory_limit_mib / new_limit, 1),
        )
        severity, priority = "MEDIUM", "P2"
        rollback = "Rollback if memory stability does not improve after 10 minutes or container restarts increase"
    elif (
        metrics.replicas > policy.min_replicas
        and metrics.cpu_avg_pct < policy.scale_down_cpu_pct
        and metrics.memory_avg_pct < policy.low_memory_pct
        and not latency_bad
        and not queue_bad
        and not errors_bad
        and metrics.ready_replicas == metrics.replicas
    ):
        target = max(policy.min_replicas, metrics.replicas - policy.scale_down_step)
        action = ResourceAction(
            action="SCALE_REPLICAS",
            **{"from": metrics.replicas, "to": target, "min": policy.min_replicas},
            max_policy=policy.max_replicas,
        )
        reasons.append("CPU and memory are below scale-down thresholds while SLO signals are healthy")
        effect = ExpectedEffect(
            cpu_target_pct=round(metrics.cpu_avg_pct * metrics.replicas / target, 1),
            memory_target_pct=metrics.memory_avg_pct,
        )
        rollback = "Rollback if p95 latency exceeds the SLO or CPU exceeds 75% during the next 10 minutes"
    else:
        reasons.append("No policy threshold justifies a safe resource change")
        if errors_bad:
            reasons.append(
                f"error rate {metrics.error_rate_pct:.1f}% blocks scale-down; investigate before changing resources"
            )
        if memory_high and not metrics.memory_limit_mib:
            reasons.append("memory is high but the current memory limit is missing")

    message_id = _identifier("MSG")
    return ResourceRecommendation(
        message_id=message_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        trace_id=trace_id,
        priority=priority,
        severity=severity,
        environment=environment,
        service=service,
        evidence_refs=evidence_refs or [],
        confidence=_confidence(metrics, window_minutes),
        idempotency_key=f"{correlation_id}:RESOURCE_RECOMMENDATION:v1",
        payload=RecommendationPayload(
            recommendation_id=recommendation_id,
            window=f"{window_minutes}m",
            current=metrics,
            forecast=Forecast(rps_expected=metrics.rps),
            recommendation=action,
            expected_effect=effect,
            reasons=reasons,
            constraints=constraints,
            rollback_condition=rollback,
        ),
    )
