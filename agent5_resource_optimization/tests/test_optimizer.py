import pytest
from pydantic import ValidationError

from agent5_resource_optimization.core.optimizer import build_recommendation
from agent5_resource_optimization.core.schemas import OptimizationPolicy, ResourceMetrics


def metrics(**overrides):
    values = {
        "replicas": 3,
        "ready_replicas": 3,
        "cpu_avg_pct": 50,
        "memory_avg_pct": 60,
        "latency_p95_ms": 250,
        "rps": 100,
        "error_rate_pct": 0,
        "queue_depth": 0,
        "memory_limit_mib": 256,
    }
    values.update(overrides)
    return ResourceMetrics(**values)


def test_scales_up_when_cpu_and_latency_are_high():
    result = build_recommendation(metrics(cpu_avg_pct=91, latency_p95_ms=890))
    action = result.payload.recommendation
    assert action.action == "SCALE_REPLICAS"
    assert action.from_replicas == 3
    assert action.to_replicas == 5
    assert result.severity == "HIGH"


def test_scale_up_never_exceeds_policy_maximum():
    policy = OptimizationPolicy(max_replicas=4)
    result = build_recommendation(metrics(replicas=4, ready_replicas=4, cpu_avg_pct=99), policy=policy)
    assert result.payload.recommendation.action == "NO_CHANGE"


def test_increases_memory_when_memory_is_high():
    result = build_recommendation(metrics(memory_avg_pct=92, memory_limit_mib=256))
    action = result.payload.recommendation
    assert action.action == "INCREASE_MEMORY"
    assert action.memory_limit_from_mib == 256
    assert action.memory_limit_to_mib == 320


def test_scales_down_one_replica_only_when_slo_is_healthy():
    result = build_recommendation(metrics(cpu_avg_pct=15, memory_avg_pct=35))
    action = result.payload.recommendation
    assert action.action == "SCALE_REPLICAS"
    assert action.to_replicas == 2


def test_does_not_scale_down_with_bad_latency():
    result = build_recommendation(metrics(cpu_avg_pct=15, memory_avg_pct=35, latency_p95_ms=900))
    assert result.payload.recommendation.action == "SCALE_REPLICAS"
    assert result.payload.recommendation.to_replicas == 5


def test_no_change_in_normal_range():
    result = build_recommendation(metrics())
    assert result.payload.recommendation.action == "NO_CHANGE"
    assert result.topic == "resource.recommendation.created"


def test_error_rate_blocks_scale_down():
    result = build_recommendation(
        metrics(cpu_avg_pct=15, memory_avg_pct=35, error_rate_pct=12)
    )
    assert result.payload.recommendation.action == "NO_CHANGE"
    assert "blocks scale-down" in result.payload.reasons[1]


def test_rejects_invalid_replica_state():
    with pytest.raises(ValidationError):
        metrics(replicas=2, ready_replicas=3)
