"""Bounded Prometheus model/worker metrics; no transaction features or credentials."""

from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from agent6_mlops_lifecycle.monitoring.state import MonitorState


def render_metrics(state: MonitorState) -> bytes:
    registry = CollectorRegistry()
    heartbeat = state.status()
    fresh = bool(
        heartbeat
        and (datetime.now(UTC) - heartbeat.polled_at).total_seconds() <= 60
        and heartbeat.state not in ("ERROR", "STOPPED")
    )
    Gauge("agent6_worker_up", "Recent monitoring heartbeat", registry=registry).set(int(fresh))
    quality = Gauge(
        "agent6_model_quality",
        "Measured quality gate: 1 pass, 0 fail, -1 unknown",
        ["model_version"],
        registry=registry,
    )
    drift = Gauge(
        "agent6_prediction_psi",
        "Prediction distribution PSI when evidence is sufficient",
        ["model_version"],
        registry=registry,
    )
    pending = Gauge(
        "agent6_pending_outcomes",
        "Predictions without independently confirmed outcomes",
        ["model_version"],
        registry=registry,
    )
    latency = Gauge(
        "agent6_inference_p95_ms",
        "Measured inference p95 in milliseconds",
        ["model_version"],
        registry=registry,
    )
    fraud_gauges = {
        name: Gauge("agent6_fraud_" + name, description, ["model_version"], registry=registry)
        for name, description in {
            "missed_cases": "Observed missed fraud cases among verified outcomes",
            "false_alarms": "Observed false alarms among verified outcomes",
            "cost_per_1000": "Configured error cost per 1000 verified transactions",
            "average_precision": "Probability average precision when evidence is supported",
            "brier_score": "Probability Brier score when evidence is supported",
            "calibration_error": "Expected calibration error when evidence is supported",
        }.items()
    }
    for version in heartbeat.active_versions if heartbeat else ():
        report = state.latest(version)
        if report is None:
            continue
        decision = report.evaluation.decision if report.evaluation else "UNKNOWN"
        quality.labels(version).set(1 if decision == "PASS" else 0 if decision == "FAIL" else -1)
        if report.drift.score is not None:
            drift.labels(version).set(report.drift.score)
        summary = report.ingestion.summary
        pending.labels(version).set(summary.available_prediction_count - summary.matched_count)
        if report.operational_health and report.operational_health.latency_p95_ms is not None:
            latency.labels(version).set(report.operational_health.latency_p95_ms)
        fraud = report.fraud_evaluation
        if fraud is not None and fraud.metrics.samples:
            fraud_gauges["missed_cases"].labels(version).set(fraud.metrics.false_negatives)
            fraud_gauges["false_alarms"].labels(version).set(fraud.metrics.false_positives)
            if fraud.metrics.cost_per_1000 is not None:
                fraud_gauges["cost_per_1000"].labels(version).set(fraud.metrics.cost_per_1000)
            if fraud.probabilities.status == "ASSESSED":
                for name, value in (
                    ("average_precision", fraud.probabilities.average_precision),
                    ("brier_score", fraud.probabilities.brier_score),
                    ("calibration_error", fraud.probabilities.expected_calibration_error),
                ):
                    if value is not None:
                        fraud_gauges[name].labels(version).set(value)
    return generate_latest(registry)
