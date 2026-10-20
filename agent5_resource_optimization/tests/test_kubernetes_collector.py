from unittest.mock import patch

from agent5_resource_optimization.collectors.kubernetes import collect_kubernetes_metrics


DEPLOYMENT = {
    "spec": {
        "replicas": 2,
        "selector": {"matchLabels": {"app": "fraud-api"}},
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "fraud-api",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "200m", "memory": "256Mi"},
                        },
                    }
                ]
            }
        },
    },
    "status": {"readyReplicas": 2},
}

POD_METRICS = {
    "items": [
        {"containers": [{"name": "fraud-api", "usage": {"cpu": "80m", "memory": "192Mi"}}]},
        {"containers": [{"name": "fraud-api", "usage": {"cpu": "120m", "memory": "224Mi"}}]},
    ]
}


@patch(
    "agent5_resource_optimization.collectors.kubernetes._kubectl_json",
    side_effect=[DEPLOYMENT, POD_METRICS],
)
def test_combines_deployment_and_metrics_api(mock_kubectl):
    result = collect_kubernetes_metrics("fraud-api", container="fraud-api")

    assert result.replicas == 2
    assert result.ready_replicas == 2
    assert result.cpu_usage_millicores == 100
    assert result.cpu_request_millicores == 100
    assert result.cpu_limit_millicores == 200
    assert result.cpu_avg_pct == 50
    assert result.memory_usage_mib == 208
    assert result.memory_request_mib == 128
    assert result.memory_limit_mib == 256
    assert result.memory_avg_pct == 81.25
    assert mock_kubectl.call_count == 2
