"""The simulated cluster as a service: same state and telemetry through HTTP."""

from fastapi.testclient import TestClient

from aiops_platform.sim.cluster import SimInspector, SimMutationAdapter
from aiops_platform.sim.remote import RemoteCluster, create_cluster_app
from aiops_platform.sim.scenarios import base_cluster, memory_leak_after_deploy
from aiops_platform.workers.agent4_executor import ClusterTargetResolver


def test_remote_cluster_matches_local_state_and_telemetry():
    local = base_cluster()
    memory_leak_after_deploy(local)
    remote = RemoteCluster(client=TestClient(create_cluster_app(local, seconds_per_minute=0)))
    assert remote.minute == local.minute
    assert remote.signals("payment-service", "prod") == local.signals("payment-service", "prod")
    assert remote.deployment("payment-service", "prod").version == "v2.7"
    assert remote.previous_release("payment-service", "prod").version == "v2.6"
    assert {d.service_id for d in remote.deployments()} == {"payment-service", "checkout-api"}


def test_agent4_boundaries_work_over_the_remote_cluster():
    local = base_cluster()
    memory_leak_after_deploy(local)
    remote = RemoteCluster(client=TestClient(create_cluster_app(local, seconds_per_minute=0)))
    target = remote.target("payment-service", "prod")
    snapshot = SimInspector(remote).inspect_deployment(target)
    assert snapshot.resource_version == str(local.deployment("payment-service", "prod").resource_version)
    op = ClusterTargetResolver(remote).typed_operation("ROLLBACK", {}, target)
    assert op.image == local.deployment("payment-service", "prod").releases["v2.6"].image

    class Artifact:
        patch_bytes_hash = "h"
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": snapshot.resource_version},
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": op.image},
        ]

    class Permit:
        patch_bytes_hash = "h"

    SimMutationAdapter(remote).apply_validated_patch(target, Artifact(), Permit())
    assert local.deployment("payment-service", "prod").version == "v2.6"
