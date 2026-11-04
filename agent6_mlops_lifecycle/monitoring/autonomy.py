"""Frozen automatic drift references and non-inference service probes."""

import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent6_mlops_lifecycle.monitoring.models import DriftBaseline
from agent6_mlops_lifecycle.monitoring.operations import feature_counts, read_operations
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import DirectObservationSource, canonical
from agent6_mlops_lifecycle.monitoring.state import MonitorState
from agent6_mlops_lifecycle.monitoring.telemetry_models import ServiceProbe


def automatic_reference(
    source: DirectObservationSource,
    state: MonitorState,
    settings: MonitoringSettings,
    version: str,
    cutoff: datetime,
) -> DriftBaseline | None:
    existing = state.baseline(version)
    if existing or settings.baseline_mode == "manual":
        return existing
    # Older predictions without operational evidence cannot certify a feature reference.
    candidates = source.first_predictions(version, 20000, cutoff)
    if len(candidates) < settings.min_distribution_samples:
        return None
    events = read_operations(source, min(row.observed_at for row in candidates), cutoff, version)
    instrumented = {event.observation_id for event in events if event.success}
    rows = tuple(row for row in candidates if row.observation_id in instrumented)[
        : settings.min_distribution_samples
    ]
    if len(rows) < settings.min_distribution_samples:
        return None
    if (cutoff - rows[0].observed_at).total_seconds() < settings.baseline_warmup_seconds:
        return None
    start = min(row.observed_at for row in rows)
    end = max(row.observed_at for row in rows) + timedelta(microseconds=1)
    if end > cutoff:
        return None
    identities = {row.observation_id for row in rows}
    events = tuple(event for event in events if event.observation_id in identities)
    baseline = DriftBaseline(
        scope=settings.scope(),
        model_version=version,
        prediction_counts=(
            sum(row.predicted == 0 for row in rows),
            sum(row.predicted == 1 for row in rows),
        ),
        prediction_source_ref="sha256:"
        + sha256(b"".join(canonical(row) + b"\n" for row in rows)).hexdigest(),
        window_start=start,
        window_end=end,
        approved_at=cutoff,
        approved_by="agent6-auto-warmup",
        note="Frozen instrumented warm-up cohort; this does not certify model quality",
        origin="AUTO_WARMUP",
        feature_counts=feature_counts(events),
    )
    return state.approve_baseline(baseline)


async def service_probes(settings: MonitoringSettings) -> dict[str, ServiceProbe]:
    if not settings.enable_service_probes:
        return {}
    parts = urlsplit(settings.prediction_url)
    urls = {"fraud-api": urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))}
    if settings.application_health_url:
        urls["fraud-application"] = settings.application_health_url
    results = {}
    async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
        for name, url in urls.items():
            started = time.perf_counter()
            try:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ValueError("Non-healthy status")
                results[name] = ServiceProbe(
                    state="UP",
                    checked_at=datetime.now(UTC),
                    response_ms=(time.perf_counter() - started) * 1000,
                )
            except (httpx.HTTPError, ValueError):
                results[name] = ServiceProbe(
                    state="DOWN",
                    checked_at=datetime.now(UTC),
                    error="Service health endpoint unavailable",
                )
    return results
