"""The two faults that make the hard cases hard, and how their outcomes are judged."""

from aiops_platform.experiments.ambiguous import Case, executed, service_cluster, snapshot, verdict
from aiops_platform.sim.cluster import SimulatedCluster


def test_cpu_burn_raises_cpu_whatever_the_traffic():
    healthy = service_cluster(None)
    burning = service_cluster("cpu_burn")
    for cluster in (healthy, burning):
        cluster.release_version("payment-service", "prod", "v2.7")
        cluster.set_load("payment-service", "prod", 260)
    gap = (
        burning.signals("payment-service", "prod")["cpu_avg_pct"]
        - healthy.signals("payment-service", "prod")["cpu_avg_pct"]
    )
    assert round(gap) == 60


def test_load_memory_follows_traffic_and_does_not_grow():
    cluster: SimulatedCluster = service_cluster("load_memory")
    cluster.release_version("payment-service", "prod", "v2.7")
    cluster.set_load("payment-service", "prod", 800)
    early = cluster.signals("payment-service", "prod")
    cluster.advance(9)
    late = cluster.signals("payment-service", "prod")
    assert early["memory_pct"] > 85
    assert early["memory_pct"] == late["memory_pct"]
    assert late["memory_slope_mb_min"] < 5


def test_a_wrong_change_reported_resolved_is_a_false_recovery():
    case = Case("cpu_burn", "x", "ROLLBACK", "cpu_burn", lambda c: None)
    assert verdict(case, True, "RESOLVED", "SCALE_REPLICAS", ["SCALE_REPLICAS"]).startswith(
        "WRONG change executed, reported RESOLVED"
    )


def test_a_wrong_proposal_that_was_not_run_is_still_reported():
    case = Case("load_memory", "x", "SCALE_REPLICAS", "load_memory", lambda c: None)
    assert verdict(case, True, "ESCALATED", "ROLLBACK", []) == "wrong action shown to a human (nothing executed)"


def test_executed_actions_are_read_from_the_state_change():
    before = {"payment-service": ("img:v2.7", 4, "v2.7"), "checkout-api": ("img:v1.4", 3, "v1.4")}
    rolled = {"payment-service": ("img:v2.6", 4, "v2.6"), "checkout-api": ("img:v1.4", 3, "v1.4")}
    scaled = {"payment-service": ("img:v2.7", 5, "v2.7"), "checkout-api": ("img:v1.4", 3, "v1.4")}
    assert executed(before, rolled) == ["ROLLBACK"]
    assert executed(before, scaled) == ["SCALE_REPLICAS"]
    assert executed(before, before) == []
    assert snapshot(service_cluster(None))["payment-service"][2] == "v2.6"
