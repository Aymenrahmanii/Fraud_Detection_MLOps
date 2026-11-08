"""Recovery verification must not call a service recovered while its CPU is still burning."""

from types import SimpleNamespace

import pytest

from aiops_platform.experiments.ambiguous import service_cluster
from aiops_platform.workers.agent1 import build_sim_monitoring

pytestmark = pytest.mark.asyncio


def context(service: str = "payment-service", namespace: str = "prod"):
    return SimpleNamespace(service_id=service, namespace=namespace, signals={"categories": ["error_rate", "latency"]})


@pytest.mark.parametrize("replicas", [5, 6], ids=["scaled to 5", "scaled to 6"])
async def test_a_scaled_cpu_burn_is_not_reported_recovered(replicas):
    # Scaling to 6 pods brings the average CPU under the 85% line; the cost per request still shows the burn.
    cluster = service_cluster("cpu_burn")
    cluster.release_version("payment-service", "prod", "v2.7")
    cluster.set_load("payment-service", "prod", 260)
    cluster.deployment("payment-service", "prod").replicas = replicas  # the scale-up that hides the bug
    _, _, _, verifier = build_sim_monitoring(cluster, sleep=None)
    result = await verifier.verify("ACT-1", context())
    assert result.checks["cpu_cost_per_request"] == "FAIL"
    assert result.status == "NOT_RECOVERED"


async def test_a_healthy_service_is_still_reported_recovered():
    cluster = service_cluster(None)
    cluster.set_load("payment-service", "prod", 300)
    _, _, _, verifier = build_sim_monitoring(cluster, sleep=None)
    result = await verifier.verify("ACT-2", context())
    assert result.checks["cpu_cost_per_request"] == "PASS"
    assert result.status == "RECOVERED"
