"""End-to-end closed loop on the in-process platform (v5 section 18 MVP boundary)."""

import json

import pytest
import pytest_asyncio

from aiops_platform.context.service import Principal
from aiops_platform.contracts.enums import Environment
from aiops_platform.contracts.topics import dlq_topic
from aiops_platform.hitl.service import Operator
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster, cpu_load_spike, memory_leak_after_deploy

pytestmark = pytest.mark.asyncio

SRE = Operator(subject="alice@sre", roles=frozenset({"sre"}))
ORCH = Principal.orchestrator()


@pytest_asyncio.fixture
async def prod(tmp_path):
    platform = await Platform.in_memory(tmp_path, base_cluster(), watched=[PAYMENT, CHECKOUT])
    yield platform
    await platform.close()


async def detect(platform):
    opened = await platform.watcher.tick()
    await platform.bus.drain(timeout=60)
    return opened


async def test_memory_leak_rollback_with_human_approval(prod):
    memory_leak_after_deploy(prod.cluster)
    [incident] = await detect(prod)

    status = await prod.orchestrator.status(incident)
    assert status["stage"] == "WAITING_FOR_APPROVAL", status.get("notes")
    assert status["selected"]["type"] == "ROLLBACK"
    assert status["consensus"]["primary_root_cause"] == "MEMORY_LEAK_POST_DEPLOY"
    assert status["policy"]["decision"] == "APPROVAL_REQUIRED"
    # Scale-up from Agent 5 was considered but scored as temporary containment.
    assert any(c["type"] == "SCALE_REPLICAS" for c in status["candidates"])
    assert prod.cluster.deployment("payment-service", "prod").version == "v2.7"  # nothing executed yet

    [approval] = await prod.pending_approvals()
    plan = approval["request_json"]["plan"]
    assert plan["status"] == "PREPARED" and plan["patch"]

    await prod.orchestrator.resolve_approval(approval["approval_id"], decision="APPROVE", operator=SRE, comment="Verify 10m")
    await prod.bus.drain(timeout=60)

    status = await prod.orchestrator.status(incident)
    assert status["stage"] == "RESOLVED", status.get("notes")
    assert status["outcome"] == "RECOVERED"
    assert prod.cluster.deployment("payment-service", "prod").version == "v2.6"
    assert prod.cluster.signals("payment-service")["memory_pct"] < 70

    ctx = await prod.context.get_incident_context(ORCH, incident)
    assert ctx.status == "RESOLVED" and ctx.closed_at is not None
    agents = {r.agent_id.value for r in ctx.agent_results}
    assert {"diagnosis-agent", "resource-optimization-agent", "deployment-recovery-agent", "monitoring-agent"} <= agents
    assert await prod.context.verify_audit_chain(ORCH, incident)
    trail = [a["action"] for a in await prod.context.audit_trail(ORCH, incident)]
    assert "APPROVAL_WAITING_FOR_HUMAN" in trail and "APPROVAL_APPROVED" in trail


async def test_rejection_replans_and_never_executes(prod):
    memory_leak_after_deploy(prod.cluster)
    [incident] = await detect(prod)
    [approval] = await prod.pending_approvals()
    await prod.orchestrator.resolve_approval(approval["approval_id"], decision="REJECT", operator=SRE, comment="collect heap evidence first")
    await prod.bus.drain(timeout=60)
    assert prod.cluster.deployment("payment-service", "prod").version == "v2.7"
    status = await prod.orchestrator.status(incident)
    assert status["replans"] == 1
    # The replanned workflow asks for approval again (new round, never auto-executed).
    assert status["stage"] == "WAITING_FOR_APPROVAL"
    assert prod.cluster.mutations == []


async def test_unauthorized_operator_cannot_approve(prod):
    memory_leak_after_deploy(prod.cluster)
    await detect(prod)
    [approval] = await prod.pending_approvals()
    with pytest.raises(Exception, match="needs one of"):
        await prod.orchestrator.resolve_approval(
            approval["approval_id"], decision="APPROVE", operator=Operator(subject="eve@ml", roles=frozenset({"ml-lead"})), comment="lgtm"
        )
    assert prod.cluster.mutations == []


async def test_bounded_scale_in_staging_runs_autonomously(tmp_path):
    platform = await Platform.in_memory(tmp_path, base_cluster(), watched=[CHECKOUT], environment=Environment.STAGING)
    try:
        cpu_load_spike(platform.cluster)
        [incident] = await detect(platform)
        status = await platform.orchestrator.status(incident)
        assert status["policy"]["decision"] == "AUTO", status["policy"]
        assert status["selected"]["type"] == "SCALE_REPLICAS"
        assert status["stage"] == "RESOLVED", status.get("notes")
        assert platform.cluster.deployment("checkout-api", "prod").replicas > 3
        assert await platform.pending_approvals() == []
    finally:
        await platform.close()


async def test_release_gate_block_stops_deployment(prod):
    assessment = {
        "assessment_id": "REL-1", "commit": "a823b91", "artifact": {"image": "registry.local/payment-service:v2.8"},
        "tests": {"total": 10, "passed": 9, "failed": 1, "skipped": 0, "coverage_pct": 80.0},
        "quality": {"gate": "PASSED"}, "security": {}, "decision": "BLOCK", "blocking_reasons": ["1 failing test"],
    }
    status = await prod.orchestrator.start_release(
        release_id="REL-payment-v2.8", service={"service_id": "payment-service", "namespace": "prod", "version": "v2.6"},
        environment="production", version="v2.8", commit="a823b91", assessment=assessment,
    )
    await prod.bus.drain(timeout=60)
    status = await prod.orchestrator.status("REL-payment-v2.8")
    assert status["stage"] == "RELEASE_BLOCKED"
    assert prod.cluster.mutations == []


async def test_release_allowed_deploys_after_approval(prod):
    assessment = {
        "assessment_id": "REL-2", "commit": "b1", "artifact": {"image": "registry.local/payment-service:v2.8"},
        "tests": {"total": 10, "passed": 10, "failed": 0, "skipped": 0, "coverage_pct": 90.0},
        "quality": {"gate": "PASSED"}, "security": {}, "decision": "ALLOW",
    }
    await prod.orchestrator.start_release(
        release_id="REL-payment-v2.8b", service={"service_id": "payment-service", "namespace": "prod", "version": "v2.6"},
        environment="production", version="v2.8", commit="b1", assessment=assessment,
    )
    await prod.bus.drain(timeout=60)
    [approval] = await prod.pending_approvals()
    assert approval["request_json"]["action"] == "DEPLOY"
    await prod.orchestrator.resolve_approval(
        approval["approval_id"], decision="APPROVE", operator=Operator(subject="rm@rel", roles=frozenset({"release-manager"})), comment="ship"
    )
    await prod.bus.drain(timeout=60)
    status = await prod.orchestrator.status("REL-payment-v2.8b")
    assert status["stage"] == "RESOLVED", status.get("notes")
    assert prod.cluster.deployment("payment-service", "prod").version == "v2.8"


async def test_model_drift_retrain_and_promotion_review(prod):
    await prod.orchestrator.start_model_check(
        check_id="MLOPS-machine-failure-1", service={"service_id": "machine-failure", "namespace": "ml", "version": "v1"}, environment="production",
    )
    await prod.bus.drain(timeout=180)
    status = await prod.orchestrator.status("MLOPS-machine-failure-1")
    health = status["results"]["model_health"]["payload"]
    assert health["decision"] == "RETRAIN_AND_EVALUATE" and health["drift_detected"]
    lifecycle = status["results"]["lifecycle"]["payload"]
    assert lifecycle["decision"] in {"REQUEST_PROMOTION_REVIEW", "KEEP_CHAMPION"}
    if lifecycle["decision"] == "REQUEST_PROMOTION_REVIEW":
        [approval] = await prod.pending_approvals()
        await prod.orchestrator.resolve_approval(
            approval["approval_id"], decision="APPROVE", operator=Operator(subject="ml@lead", roles=frozenset({"ml-lead"})), comment="promote"
        )
        await prod.bus.drain(timeout=60)
        status = await prod.orchestrator.status("MLOPS-machine-failure-1")
        assert status["stage"] == "PROMOTION_APPROVED"
    else:
        assert status["stage"] == "KEEP_CHAMPION"


async def test_duplicate_delivery_has_no_second_side_effect(prod):
    memory_leak_after_deploy(prod.cluster)
    await detect(prod)
    [approval] = await prod.pending_approvals()
    await prod.orchestrator.resolve_approval(approval["approval_id"], decision="APPROVE", operator=SRE, comment="ok")
    await prod.bus.drain(timeout=60)
    # Kafka redelivers the continuation and the anomaly: nothing must happen twice.
    for topic in ("workflow.continuation", "monitoring.anomaly.detected"):
        for record in prod.bus.topic_records(topic):
            await prod.bus.redeliver(record)
    await prod.bus.drain(timeout=60)
    assert len(prod.cluster.mutations) == 1
    dead = [json.loads(r.value) for r in prod.bus.topic_records(dlq_topic("workflow.continuation"))]
    assert dead == []
