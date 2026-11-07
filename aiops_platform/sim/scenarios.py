"""Repeatable fault-injection scenarios (v5 section 18 "Suggested scenarios")."""

from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.sim.cluster import Release, SimulatedCluster, digest_image

PAYMENT = ServiceIdentity(service_id="payment-service", namespace="prod", version="v2.7")
CHECKOUT = ServiceIdentity(service_id="checkout-api", namespace="prod", version="v1.4")


def base_cluster() -> SimulatedCluster:
    cluster = SimulatedCluster()
    repo = "registry.local/payment-service"
    cluster.add_service(
        "payment-service", "prod",
        [
            Release("v2.6", digest_image(repo, "v2.6")),
            Release("v2.7", digest_image(repo, "v2.7"), frozenset({"memory_leak"})),
            Release("v2.8", digest_image(repo, "v2.8")),
        ],
        current="v2.6", replicas=4,
    )
    repo = "registry.local/checkout-api"
    cluster.add_service(
        "checkout-api", "prod",
        [Release("v1.3", digest_image(repo, "v1.3")), Release("v1.4", digest_image(repo, "v1.4"))],
        current="v1.4", replicas=3, rps=250,
    )
    return cluster


def memory_leak_after_deploy(cluster: SimulatedCluster, minutes: float = 9) -> None:
    """v5 scenario 1: a deployment introduces a memory leak."""
    cluster.release_version("payment-service", "prod", "v2.7")
    cluster.advance(minutes)


def cpu_load_spike(cluster: SimulatedCluster, rps: float = 640) -> None:
    """v5 scenario 2: CPU saturation caused by a controlled load spike."""
    cluster.set_load("checkout-api", "prod", rps)
    cluster.advance(5)
