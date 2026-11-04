"""Read-only serving telemetry, operational thresholds and feature PSI."""

import math
from collections import Counter
from datetime import datetime
from hashlib import sha256

from agent6_mlops_lifecycle.contracts.mlops import DistributionComparison
from agent6_mlops_lifecycle.health.metrics import population_stability_index
from agent6_mlops_lifecycle.monitoring.models import DriftBaseline
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    SourceUnavailableError,
    utc,
)
from agent6_mlops_lifecycle.monitoring.telemetry_models import (
    FEATURE_NAMES,
    FeatureShift,
    OperationalHealth,
    ServingEvent,
)


def read_operations(
    source: DirectObservationSource, start: datetime, end: datetime, version: str | None = None
) -> tuple[ServingEvent, ...]:
    if not source.database.is_file():
        return ()
    with source._connection() as connection:
        connection.execute("BEGIN")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent6_serving_events_v1'"
        ).fetchone():
            return ()
        rows = connection.execute(
            "SELECT event_id,tenant_id,project_id,model_id,model_version,observed_at,"
            "CASE WHEN length(payload)<=4096 THEN payload ELSE NULL END AS payload,digest "
            "FROM agent6_serving_events_v1 WHERE tenant_id=? AND project_id=? AND model_id=? "
            "AND observed_at>=? AND observed_at<? "
            + ("AND model_version=? " if version else "")
            + "ORDER BY observed_at,event_id LIMIT 20001",
            (
                source.scope.tenant_id,
                source.scope.project_id,
                source.scope.model_id,
                utc(start),
                utc(end),
            )
            + ((version,) if version else ()),
        ).fetchall()
    if len(rows) > 20000:
        raise SourceUnavailableError("Serving window exceeds 20,000 events; narrow the window")
    events = []
    for row in rows:
        if row["payload"] is None or sha256(bytes(row["payload"])).hexdigest() != row["digest"]:
            raise SourceUnavailableError("Serving telemetry integrity check failed")
        try:
            event = ServingEvent.model_validate_json(bytes(row["payload"]))
            if (
                any(
                    getattr(event, name) != row[name]
                    for name in ("event_id", "tenant_id", "project_id", "model_id", "model_version")
                )
                or utc(event.observed_at) != row["observed_at"]
            ):
                raise ValueError("Indexed telemetry identity differs from its evidence")
        except ValueError as error:
            raise SourceUnavailableError("Serving telemetry contract is invalid") from error
        if event.recorded_at <= end:
            events.append(event)
    return tuple(events)


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    index = (len(values) - 1) * q
    lower, upper = math.floor(index), math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def health(
    events: tuple[ServingEvent, ...],
    version: str,
    start: datetime,
    end: datetime,
    predictions: int,
    settings: MonitoringSettings,
) -> OperationalHealth:
    attempts = len(events)
    failures = sum(not row.success for row in events)
    successes = len({row.observation_id for row in events if row.success})
    issues = sum(
        any(value in ("MISSING", "INVALID") for value in row.feature_bins.values())
        for row in events
    )
    # Validation rejections have no model inference duration and are excluded from latency.
    latencies = [row.duration_ms for row in events if row.status_code != 422]
    p50 = percentile(latencies, 0.5) if latencies else None
    p95 = percentile(latencies, 0.95) if latencies else None
    eligible = attempts >= settings.min_operational_samples
    reasons = []
    if eligible:
        if failures / attempts > settings.max_failure_rate:
            reasons.append("ERROR_RATE_HIGH")
        if issues / attempts > settings.max_input_issue_rate:
            reasons.append("INPUT_QUALITY")
        if p95 is not None and p95 > settings.max_latency_p95_ms:
            reasons.append("LATENCY_HIGH")
    return OperationalHealth(
        model_version=version,
        window_start=start,
        window_end=end,
        status="NO_TELEMETRY"
        if not attempts
        else "COLLECTING"
        if not eligible
        else "DEGRADED"
        if reasons
        else "HEALTHY",
        attempts=attempts,
        failures=failures,
        failure_rate=failures / attempts if attempts else None,
        input_issue_rate=issues / attempts if attempts else None,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        successful_predictions=successes,
        tracked_predictions=predictions,
        telemetry_coverage=min(1.0, successes / predictions) if predictions else None,
        reasons=tuple(reasons),
    )


def feature_counts(events: tuple[ServingEvent, ...]) -> dict[str, dict[str, int]]:
    return {
        name: dict(Counter(row.feature_bins[name] for row in events if row.success))
        for name in sorted(FEATURE_NAMES)
    }


def feature_shift(
    events: tuple[ServingEvent, ...], baseline: DriftBaseline | None, settings: MonitoringSettings
) -> FeatureShift:
    if baseline is None:
        return FeatureShift(status="WARMING_UP", threshold=settings.psi_threshold)
    reference = baseline.feature_counts
    current = feature_counts(events)
    ref_n = min((sum(bins.values()) for bins in reference.values()), default=0)
    current_n = sum(row.success for row in events)
    if not ref_n or not current_n:
        return FeatureShift(
            status="NO_TELEMETRY",
            threshold=settings.psi_threshold,
            reference_samples=ref_n,
            current_samples=current_n,
        )
    if min(ref_n, current_n) < settings.min_distribution_samples:
        return FeatureShift(
            status="INSUFFICIENT_EVIDENCE",
            threshold=settings.psi_threshold,
            reference_samples=ref_n,
            current_samples=current_n,
        )
    scores = {}
    for name in sorted(FEATURE_NAMES & reference.keys()):
        keys = sorted(set(reference[name]) | set(current[name]))
        # A constant single bucket has no measured shift.
        if len(keys) < 2:
            scores[name] = 0.0
        else:
            scores[name] = population_stability_index(
                DistributionComparison(
                    name=name,
                    scope="feature",
                    bins=tuple(keys),
                    reference_counts=tuple(reference[name].get(key, 0) for key in keys),
                    current_counts=tuple(current[name].get(key, 0) for key in keys),
                )
            )
    detected = tuple(name for name, score in scores.items() if score >= settings.psi_threshold)
    return FeatureShift(
        status="DETECTED" if detected else "STABLE",
        scores=scores,
        detected_features=detected,
        threshold=settings.psi_threshold,
        reference_samples=ref_n,
        current_samples=current_n,
    )
