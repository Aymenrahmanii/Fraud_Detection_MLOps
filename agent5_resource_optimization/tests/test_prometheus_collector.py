from unittest.mock import patch

from agent5_resource_optimization.collectors.prometheus import PrometheusCollector


@patch.object(PrometheusCollector, "_query", side_effect=[0.25, 12.5, 0.02])
def test_collects_application_metrics(mock_query):
    result = PrometheusCollector("http://prometheus:9090").collect(window_minutes=15)

    assert result.latency_p95_ms == 250
    assert result.rps == 12.5
    assert result.error_rate_pct == 2
    assert mock_query.call_count == 3
    assert "http_request_duration_seconds_bucket" in mock_query.call_args_list[0].args[0]
    assert "http_requests_total" in mock_query.call_args_list[1].args[0]


@patch.object(PrometheusCollector, "_query", side_effect=[0.1, 3.0, None])
def test_missing_error_series_means_zero_when_requests_exist(mock_query):
    result = PrometheusCollector("http://prometheus:9090").collect()
    assert result.error_rate_pct == 0
