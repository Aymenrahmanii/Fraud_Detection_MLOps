"""Context/HITL/Monitoring over real gRPC: identities, access rules, sync clients."""

import asyncio
from datetime import UTC, datetime

import grpc
import pytest
import pytest_asyncio

from aiops_platform.context.client import AgentContext, OrchestratorContext
from aiops_platform.context.service import AccessDenied, Principal
from aiops_platform.contracts.enums import AgentId, Environment, Priority, ResultStatus, Severity
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult
from aiops_platform.hitl.keys import SigningKey
from aiops_platform.hitl.service import ApprovalRequest, HitlError, HitlService, Operator
from aiops_platform.rpc.context_rpc import GrpcContextService, add_context_service
from aiops_platform.rpc.hitl_rpc import GrpcHitlClient, SyncHitlProvenance, add_hitl_service
from aiops_platform.rpc.identity import Credential, Verifier, issue_token

pytestmark = pytest.mark.asyncio
SECRET = "test-secret"


def cred(principal):
    return Credential(principal, issue_token(SECRET, principal))


@pytest_asyncio.fixture
async def stack(context):
    server = grpc.aio.server()
    add_context_service(server, context, Verifier(SECRET))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    target = f"127.0.0.1:{port}"
    channel = grpc.aio.insecure_channel(target)
    yield target, channel
    await channel.close()
    await server.stop(0)


async def test_access_rules_hold_across_the_wire(stack):
    _, channel = stack
    orch = OrchestratorContext(GrpcContextService(channel, cred(Principal.orchestrator())))
    ctx, created = await orch.open_incident(
        incident_id="INC-RPC-1",
        kind="INCIDENT",
        service=ServiceIdentity(service_id="svc", namespace="prod", version="v1"),
        environment=Environment.PRODUCTION,
        severity=Severity.HIGH,
        priority=Priority.P1,
        signals={"m": 1},
    )
    assert created and ctx.incident_id == "INC-RPC-1"

    diag = AgentContext(GrpcContextService(channel, cred(Principal.agent(AgentId.DIAGNOSIS))), AgentId.DIAGNOSIS)
    result = AgentResult(
        result_id="R1",
        task_id="T1",
        incident_id="INC-RPC-1",
        agent_id=AgentId.DIAGNOSIS,
        status=ResultStatus.SUCCEEDED,
        rationale_summary="ok",
    )
    assert await diag.append_agent_result(result, "DIAGNOSIS_COMPLETED")
    ref = await diag.append_evidence(
        incident_id="INC-RPC-1", evidence_type="metric", summary="m", content={"m": 1}, observed_at=datetime.now(UTC)
    )
    assert ref.source == "diagnosis-agent"

    # An agent has no read endpoint, even over gRPC.
    raw = GrpcContextService(channel, cred(Principal.agent(AgentId.DIAGNOSIS)))
    with pytest.raises(AccessDenied):
        await raw.get_incident_context(Principal.agent(AgentId.DIAGNOSIS), "INC-RPC-1")
    # A client cannot speak for another identity.
    with pytest.raises(AccessDenied):
        await raw.append_agent_result(Principal.agent(AgentId.MONITORING), result, "X")

    full = await orch.get_incident_context("INC-RPC-1")
    assert [r.agent_id for r in full.agent_results] == [AgentId.DIAGNOSIS]
    assert await orch.verify_audit_chain("INC-RPC-1")


async def test_context_outage_surfaces_as_transient_through_hitl(context):
    """Regression: an outage reached the Dashboard as INTERNAL/500 instead of retryable."""
    from aiops_platform.bus.base import TransientError

    dead_channel = grpc.aio.insecure_channel("127.0.0.1:1")  # nothing listens: UNAVAILABLE
    hitl = HitlService(
        context=GrpcContextService(dead_channel, cred(Principal.hitl())),
        signer=SigningKey.generate("k", "aiops-hitl-service"),
    )
    server = grpc.aio.server()
    add_hitl_service(server, hitl, Verifier(SECRET))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        client = GrpcHitlClient(grpc.aio.insecure_channel(f"127.0.0.1:{port}"), cred(Principal.orchestrator()))
        with pytest.raises(TransientError):
            await client.get_approval_state("APR-ANY")
    finally:
        await server.stop(0)
        await dead_channel.close()


async def test_forged_identity_is_unauthenticated(stack):
    _, channel = stack
    forged = Credential(Principal.orchestrator(), issue_token("wrong-secret", Principal.orchestrator()))
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await GrpcContextService(channel, forged).list_incidents(Principal.orchestrator())
    assert err.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_hitl_over_grpc_and_sync_provenance(stack):
    target, channel = stack
    hitl = HitlService(
        context=GrpcContextService(channel, cred(Principal.hitl())),
        signer=SigningKey.generate("k1", "aiops-hitl-service"),
    )
    server = grpc.aio.server()
    add_hitl_service(server, hitl, Verifier(SECRET))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    hitl_target = f"127.0.0.1:{port}"
    try:
        orch_ctx = OrchestratorContext(GrpcContextService(channel, cred(Principal.orchestrator())))
        await orch_ctx.open_incident(
            incident_id="INC-RPC-2",
            kind="MLOPS",
            service=ServiceIdentity(service_id="m", namespace="ml", version="v1"),
            environment=Environment.PRODUCTION,
            severity=Severity.MEDIUM,
            priority=Priority.P2,
            signals={},
        )
        hitl_channel = grpc.aio.insecure_channel(hitl_target)
        client = GrpcHitlClient(hitl_channel, cred(Principal.orchestrator()))
        request = ApprovalRequest(
            approval_id="APR-RPC-1",
            incident_id="INC-RPC-2",
            correlation_id="INC-RPC-2",
            environment="production",
            priority="P2",
            severity="MEDIUM",
            action_id="ACT-1",
            action_version=1,
            action="PROMOTE_MODEL",
            parameters={"version": "2"},
            risk={"level": "MEDIUM"},
            decision_confidence=1.0,
            reason="candidate qualified",
        )
        state = await client.create_approval(request)
        assert state.state == "WAITING_FOR_HUMAN"
        with pytest.raises(HitlError) as err:
            await client.resolve_approval(
                "APR-RPC-1",
                decision="APPROVE",
                operator=Operator(subject="x@sre", roles=frozenset({"sre"})),
                comment="no",
            )
        assert err.value.code == "APPROVER_NOT_AUTHORIZED"
        state = await client.resolve_approval(
            "APR-RPC-1",
            decision="APPROVE",
            operator=Operator(subject="ml@lead", roles=frozenset({"ml-lead"})),
            comment="promote",
        )
        assert state.state == "APPROVED"

        # Agent 4 reads provenance with a blocking client from its worker thread.
        lookup = SyncHitlProvenance(hitl_target, cred(Principal.agent(AgentId.DEPLOYMENT_RECOVERY)))
        row = await asyncio.to_thread(lookup, "APR-RPC-1")
        assert row["state"] == "APPROVED" and row["approver_id"] == "ml@lead"
        # ... but another agent may not.
        other = SyncHitlProvenance(hitl_target, cred(Principal.agent(AgentId.DIAGNOSIS)))
        with pytest.raises(grpc.RpcError):
            await asyncio.to_thread(other, "APR-RPC-1")
        await hitl_channel.close()
    finally:
        await server.stop(0)
