"""Read application SLI values from the Prometheus HTTP API."""

import json
import math
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class PrometheusMetrics:
    latency_p95_ms: float | None
    rps: float | None
    error_rate_pct: float | None


class PrometheusCollector:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _query(self, expression: str) -> float | None:
        url = f"{self.base_url}/api/v1/query?{urlencode({'query': expression})}"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Prometheus query failed at {self.base_url}: {exc}") from exc

        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus rejected query: {payload.get('error', 'unknown error')}")
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        value = float(results[0]["value"][1])
        return None if not math.isfinite(value) else value

    def collect(self, *, window_minutes: int = 15, handler: str = "/predict") -> PrometheusMetrics:
        window = f"{window_minutes}m"
        latency_seconds = self._query(
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(http_request_duration_seconds_bucket{{handler=\"{handler}\"}}[{window}])))"
        )
        rps = self._query(f'sum(rate(http_requests_total{{handler="{handler}"}}[{window}]))')
        error_ratio = self._query(
            f'sum(rate(http_requests_total{{handler="{handler}",status=~"5.."}}[{window}])) '
            f'/ clamp_min(sum(rate(http_requests_total{{handler="{handler}"}}[{window}])), 1e-9)'
        )
        if error_ratio is None and rps is not None:
            error_ratio = 0.0
        return PrometheusMetrics(
            latency_p95_ms=round(latency_seconds * 1000, 3) if latency_seconds is not None else None,
            rps=round(rps, 6) if rps is not None else None,
            error_rate_pct=round(error_ratio * 100, 4) if error_ratio is not None else None,
        )
