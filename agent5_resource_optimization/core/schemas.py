"""Typed RESOURCE_RECOMMENDATION contract from the six-agent architecture."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


Action = Literal["NO_CHANGE", "SCALE_REPLICAS", "INCREASE_MEMORY"]


class ServiceRef(BaseModel):
    service_id: str = "fraud-api"
    namespace: str = "default"
    version: str = "unknown"


class ResourceMetrics(BaseModel):
    replicas: int = Field(ge=1)
    ready_replicas: int = Field(ge=0)
    cpu_avg_pct: float = Field(ge=0)
    memory_avg_pct: float = Field(ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    rps: float | None = Field(default=None, ge=0)
    error_rate_pct: float | None = Field(default=None, ge=0, le=100)
    queue_depth: int | None = Field(default=None, ge=0)
    cpu_usage_millicores: float | None = Field(default=None, ge=0)
    cpu_request_millicores: float | None = Field(default=None, ge=0)
    cpu_limit_millicores: float | None = Field(default=None, gt=0)
    memory_usage_mib: float | None = Field(default=None, ge=0)
    memory_request_mib: float | None = Field(default=None, ge=0)
    memory_limit_mib: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ready_cannot_exceed_desired(self):
        if self.ready_replicas > self.replicas:
            raise ValueError("ready_replicas cannot exceed replicas")
        return self


class Forecast(BaseModel):
    horizon: str = "10m"
    rps_expected: float | None = None
    confidence_interval: list[float] = Field(default_factory=list)


class ResourceAction(BaseModel):
    action: Action
    from_replicas: int | None = Field(default=None, alias="from")
    to_replicas: int | None = Field(default=None, alias="to")
    min_replicas: int = Field(default=1, alias="min")
    max_policy: int = 8
    memory_limit_from_mib: float | None = None
    memory_limit_to_mib: float | None = None

    model_config = {"populate_by_name": True}


class ExpectedEffect(BaseModel):
    latency_reduction_pct: float = Field(default=0, ge=0, le=100)
    cpu_target_pct: float | None = Field(default=None, ge=0)
    memory_target_pct: float | None = Field(default=None, ge=0)


class RecommendationPayload(BaseModel):
    recommendation_id: str
    window: str
    current: ResourceMetrics
    forecast: Forecast = Field(default_factory=Forecast)
    recommendation: ResourceAction
    expected_effect: ExpectedEffect
    reasons: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    rollback_condition: str


class ResourceRecommendation(BaseModel):
    message_id: str
    schema_version: str = "1.0"
    correlation_id: str
    causation_id: str | None = None
    trace_id: str
    source: str = "resource-optimization-agent"
    target: str = "orchestrator"
    topic: str = "resource.recommendation.created"
    message_type: str = "RESOURCE_RECOMMENDATION"
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str = "development"
    service: ServiceRef = Field(default_factory=ServiceRef)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_ack: bool = True
    idempotency_key: str
    payload: RecommendationPayload


class OptimizationPolicy(BaseModel):
    min_replicas: int = Field(default=1, ge=1)
    max_replicas: int = Field(default=8, ge=1)
    scale_up_cpu_pct: float = Field(default=75, ge=0)
    scale_down_cpu_pct: float = Field(default=30, ge=0)
    high_memory_pct: float = Field(default=85, ge=0)
    low_memory_pct: float = Field(default=50, ge=0)
    latency_slo_ms: float = Field(default=500, gt=0)
    high_queue_depth: int = Field(default=20, ge=0)
    max_error_rate_pct: float = Field(default=5, ge=0, le=100)
    scale_up_factor: float = Field(default=1.5, gt=1)
    scale_down_step: int = Field(default=1, ge=1)
    memory_increase_factor: float = Field(default=1.25, gt=1)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas cannot exceed max_replicas")
        if self.scale_down_cpu_pct >= self.scale_up_cpu_pct:
            raise ValueError("scale_down_cpu_pct must be below scale_up_cpu_pct")
        if self.low_memory_pct >= self.high_memory_pct:
            raise ValueError("low_memory_pct must be below high_memory_pct")
        return self
