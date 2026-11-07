"""HITL permit -> Agent 4's real coordinator -> signed Agent 1 verdict, on the simulated cluster."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from aiops_platform.context.service import Principal
from aiops_platform.hitl.keys import SigningKey, registry_for
from aiops_platform.hitl.service import ApprovalRequest, ApproverNotAuthorized, HitlService, Operator
from aiops_platform.sim.cluster import Release, SimInspector, SimMutationAdapter, SimulatedCluster, digest_image
from aiops_platform.tests.conftest import open_incident
from aiops_platform.workers.agent1 import Agent1VerdictService, build_sim_monitoring
from aiops_platform.workers.agent4_executor import Agent1VerdictProvider, Agent4Executor, ClusterTargetResolver

pytestmark = pytest.mark.asyncio

SRE = Operator(subject="alice@sre", roles=frozenset({"sre"}))


def leaking_cluster():
    cluster = SimulatedCluster()
    repo = "registry.local/payment-service"
    cluster.add_service(
        "payment-service",
        "prod",
        [
            Release("v2.6", digest_image(repo, "v2.6")),
            Release("v2.7", digest_image(repo, "v2.7"), frozenset({"memory_leak"})),
        ],
        current="v2.6",
    )
    cluster.release_version("payment-service", "prod", "v2.7")
    cluster.advance(9)
    return cluster


@pytest.fixture
def setup(tmp_path, context):
    cluster = leaking_cluster()
    hitl_key = SigningKey.generate("hitl-key-1", "aiops-hitl-service")
    agent1_key = SigningKey.generate("agent1-key-1", "agent1-monitoring")
    _, _, _, verifier = build_sim_monitoring(cluster)
    verdicts = Agent1VerdictProvider(Agent1VerdictService(verifier, agent1_key).sync_provider())
    hitl = HitlService(context=context, signer=hitl_key)
    loop = asyncio.get_event_loop()

    def lookup(approval_id):
        # Agent 4 runs in a worker thread; bridge to the event loop owning the store.
        return asyncio.run_coroutine_threadsafe(hitl.provenance_record(approval_id), loop).result(10)

    executor = Agent4Executor(
        journal_path=tmp_path / "agent4.db",
        inspector=SimInspector(cluster),
        mutation_adapter=SimMutationAdapter(cluster),
        resolver=ClusterTargetResolver(cluster),
        preflight_policy=cluster.preflight_policy,
        hitl_registry=registry_for(hitl_key),
        agent1_registry=registry_for(agent1_key),
        approval_lookup=lookup,
        verdicts=verdicts,
    )
    return cluster, hitl, executor


async def in_thread(fn, *args, **kwargs):
    with ThreadPoolExecutor(1) as pool:
        return await asyncio.get_running_loop().run_in_executor(pool, lambda: fn(*args, **kwargs))


def request_for(plan, environment="production"):
    return ApprovalRequest(
        approval_id="APR-TEST-001",
        incident_id="INC-TEST-0001",
        correlation_id="INC-TEST-0001",
        environment=environment,
        priority="P1",
        severity="HIGH",
        action_id="ACT-001",
        action_version=1,
        action="ROLLBACK",
        parameters={"to_version": "v2.6"},
        risk={"level": "HIGH", "score": 0.6},
        decision_confidence=0.86,
        reason="memory leak after v2.7",
        plan=plan,
    )


async def prepare(executor, environment="production"):
    return await in_thread(
        executor.prepare,
        incident_id="INC-TEST-0001",
        trace_id="TRACE-INC-TEST-0001",
        environment=environment,
        service_id="payment-service",
        namespace="prod",
        action_id="ACT-001",
        action_version=1,
        action="ROLLBACK",
        parameters={"to_version": "v2.6"},
    )


async def test_human_approved_production_rollback_executes_and_recovers(setup, context):
    cluster, hitl, executor = setup
    await open_incident(context)
    assert cluster.signals("payment-service")["memory_pct"] > 75
    plan = await prepare(executor)
    assert plan.status == "PREPARED", plan.reason
    assert any(op["op"] == "replace" and op["path"].endswith("/image") for op in plan.patch)

    orchestrator = Principal.orchestrator()
    await hitl.create_approval(orchestrator, request_for(plan))
    with pytest.raises(ApproverNotAuthorized):
        await hitl.resolve_approval(
            orchestrator, "APR-TEST-001", decision="APPROVE",
            operator=Operator(subject="bob@ml", roles=frozenset({"ml-lead"})), comment="ok",
        )
    state = await hitl.resolve_approval(orchestrator, "APR-TEST-001", decision="APPROVE", operator=SRE, comment="Verify 10m")
    assert state.state == "APPROVED" and state.approval_token

    outcome = await in_thread(executor.execute, plan, receipt=state.receipt, approval_token=state.approval_token)
    assert outcome.status == "SUCCESS", outcome.reason
    assert outcome.state == "SUCCEEDED"
    assert outcome.pre_state["version"] == "v2.7" and outcome.post_state["version"] == "v2.6"
    assert cluster.signals("payment-service")["memory_pct"] < 70
    assert executor.audit_chain_valid(outcome.execution_id)


async def test_policy_permit_cannot_mutate_production(setup, context):
    cluster, hitl, executor = setup
    await open_incident(context)
    plan = await prepare(executor)
    state = await hitl.issue_policy_permit(Principal.orchestrator(), request_for(plan))
    outcome = await in_thread(executor.execute, plan, receipt=state.receipt, approval_token=state.approval_token)
    assert outcome.status != "SUCCESS"
    assert "human" in outcome.reason, outcome.reason
    assert cluster.deployment("payment-service", "prod").version == "v2.7"  # nothing changed


async def test_cluster_change_after_approval_is_refused(setup, context):
    cluster, hitl, executor = setup
    await open_incident(context)
    plan = await prepare(executor)
    orchestrator = Principal.orchestrator()
    await hitl.create_approval(orchestrator, request_for(plan))
    state = await hitl.resolve_approval(orchestrator, "APR-TEST-001", decision="APPROVE", operator=SRE, comment="ok")
    cluster.set_load("payment-service", "prod", 400)
    cluster.apply_patch("prod", "payment-service", [])  # someone else bumped the Deployment
    outcome = await in_thread(executor.execute, plan, receipt=state.receipt, approval_token=state.approval_token)
    assert outcome.status != "SUCCESS"
    assert outcome.reason == "RESOURCE_VERSION_MISMATCH", outcome.reason  # Agent 4 preflight
    assert cluster.deployment("payment-service", "prod").version == "v2.7"


async def test_rejected_approval_signs_nothing(setup, context):
    _, hitl, executor = setup
    await open_incident(context)
    plan = await prepare(executor)
    orchestrator = Principal.orchestrator()
    await hitl.create_approval(orchestrator, request_for(plan))
    state = await hitl.resolve_approval(orchestrator, "APR-TEST-001", decision="REJECT", operator=SRE, comment="need heap")
    assert state.state == "REJECTED" and state.approval_token is None


async def test_unresolvable_intent_is_rejected_before_approval(setup, context):
    _, _, executor = setup
    plan = await in_thread(
        executor.prepare,
        incident_id="INC-TEST-0001", trace_id="TRACE-1", environment="production",
        service_id="payment-service", namespace="prod", action_id="ACT-002", action_version=1,
        action="ROLLBACK", parameters={"to_version": "v9.9"},
    )
    assert plan.status == "REJECTED" and "unknown release" in plan.reason
