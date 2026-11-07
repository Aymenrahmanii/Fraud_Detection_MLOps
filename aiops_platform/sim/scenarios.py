"""Repeatable fault-injection scenarios (v5 section 18 "Suggested scenarios")."""

import time
from datetime import UTC, datetime
from typing import Any

from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.sim.cluster import Release, SimulatedCluster, digest_image

PAYMENT = ServiceIdentity(service_id="payment-service", namespace="prod", version="v2.7")
CHECKOUT = ServiceIdentity(service_id="checkout-api", namespace="prod", version="v1.4")


def base_cluster() -> SimulatedCluster:
    cluster = SimulatedCluster()
    repo = "registry.local/payment-service"
    cluster.add_service(
        "payment-service",
        "prod",
        [
            Release("v2.6", digest_image(repo, "v2.6")),
            Release("v2.7", digest_image(repo, "v2.7"), frozenset({"memory_leak"})),
            Release("v2.8", digest_image(repo, "v2.8")),
        ],
        current="v2.6",
        replicas=4,
    )
    repo = "registry.local/checkout-api"
    cluster.add_service(
        "checkout-api",
        "prod",
        [Release("v1.3", digest_image(repo, "v1.3")), Release("v1.4", digest_image(repo, "v1.4"))],
        current="v1.4",
        replicas=3,
        rps=250,
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


def release_assessment(fixed: bool, assessment_id: str, commit: str) -> dict[str, Any]:
    """Agent 3's assessment of payment-service v2.8: one failing test, or the fixed build."""
    return {
        "assessment_id": assessment_id,
        "commit": commit,
        "artifact": {"image": "registry.local/payment-service:v2.8"},
        "tests": {
            "total": 124,
            "passed": 124 if fixed else 123,
            "failed": 0 if fixed else 1,
            "skipped": 0,
            "coverage_pct": 87.3 if fixed else 86.0,
        },
        "quality": {"gate": "PASSED"},
        "security": {},
        "decision": "ALLOW" if fixed else "BLOCK",
        "blocking_reasons": [] if fixed else ["1 failing test"],
    }


def release_v28_request(cluster: SimulatedCluster, fixed: bool) -> dict[str, Any]:
    """`start_release` arguments for v5 scenario 5: payment-service v2.8 through Agent 3's gate.

    Each call is a new workflow (timestamped IDs), from the version currently running.
    """
    stamp = int(time.time() * 1000)
    commit = f"{'c0ffee2' if fixed else 'c0ffee1'}-{stamp}"
    current = cluster.deployment(PAYMENT.service_id, PAYMENT.namespace).version
    return {
        "release_id": f"REL-payment-v2.8-{'fixed' if fixed else 'failing-test'}-{stamp}",
        "service": {"service_id": PAYMENT.service_id, "namespace": PAYMENT.namespace, "version": current},
        "environment": "production",
        "version": "v2.8",
        "commit": commit,
        "assessment": release_assessment(fixed, f"REL-DEMO-{stamp}", commit),
    }


def model_check_request() -> dict[str, Any]:
    """`start_model_check` arguments for v5 scenario 6: drift on the machine-failure model."""
    return {
        "check_id": f"MLOPS-machine-failure-{datetime.now(UTC):%Y%m%d%H%M%S%f}"[:48],
        "service": {"service_id": "machine-failure", "namespace": "ml", "version": "v1"},
        "environment": "production",
    }
