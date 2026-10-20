import json
from unittest.mock import Mock, patch

from agent5_resource_optimization.collectors.docker import collect_docker_metrics


@patch("agent5_resource_optimization.collectors.docker.subprocess.run")
def test_parses_docker_stats(mock_run):
    snapshot = {
        "CPUPerc": "2.50%",
        "MemPerc": "91.17%",
        "MemUsage": "233.4MiB / 256MiB",
    }
    mock_run.return_value = Mock(returncode=0, stdout=json.dumps(snapshot), stderr="")

    result = collect_docker_metrics("fraud-api", latency_p95_ms=220, rps=12)

    assert result.cpu_avg_pct == 2.5
    assert result.memory_avg_pct == 91.17
    assert result.memory_usage_mib == 233.4
    assert result.memory_limit_mib == 256
    assert result.latency_p95_ms == 220
