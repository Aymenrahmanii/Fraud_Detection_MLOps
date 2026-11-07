"""Context Service (v5 section 6): orchestrator-read / agent-append, audit chain."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from aiops_platform.context import schema as t
from aiops_platform.context.service import AccessDenied, Principal
from aiops_platform.contracts.context import CandidateAction
from aiops_platform.contracts.enums import AgentId, ResultStatus
from aiops_platform.contracts.tasks import AgentResult
from aiops_platform.tests.conftest import open_incident

pytestmark = pytest.mark.asyncio

DIAG = Principal.agent(AgentId.DIAGNOSIS)


def result(agent=AgentId.DIAGNOSIS, task="TASK-1", incident="INC-TEST-0001"):
    return AgentResult(
        result_id=f"RES-{agent.value}-{task}",
        task_id=task,
        incident_id=incident,
        agent_id=agent,
        status=ResultStatus.SUCCEEDED,
        rationale_summary="memory leak after v2.7",
        confidence=0.86,
        payload={"primary_root_cause": "MEMORY_LEAK_POST_DEPLOY"},
    )


async def test_open_incident_is_idempotent(context, orchestrator):
    first = await open_incident(context)
    again = await open_incident(context)
    assert first.incident_id == again.incident_id
    assert len(await context.list_incidents(orchestrator)) == 1


async def test_agents_cannot_read_context(context):
    await open_incident(context)
    with pytest.raises(AccessDenied):
        await context.get_incident_context(DIAG, "INC-TEST-0001")
    with pytest.raises(AccessDenied):
        await context.get_latest_agent_results(DIAG, "INC-TEST-0001")


async def test_agent_appends_only_its_own_result(context, orchestrator):
    await open_incident(context)
    assert await context.append_agent_result(DIAG, result(), "DIAGNOSIS_COMPLETED")
    with pytest.raises(AccessDenied):
        await context.append_agent_result(DIAG, result(agent=AgentId.MONITORING), "ANOMALY_DETECTED")
    # A redelivered append for the same task is a no-op.
    assert not await context.append_agent_result(DIAG, result(), "DIAGNOSIS_COMPLETED")
    latest = await context.get_latest_agent_results(orchestrator, "INC-TEST-0001")
    assert [r.agent_id for r in latest] == [AgentId.DIAGNOSIS]
    assert latest[0].payload["primary_root_cause"] == "MEMORY_LEAK_POST_DEPLOY"


async def test_evidence_is_content_addressed_and_deduplicated(context, orchestrator):
    await open_incident(context)
    now = datetime.now(UTC)
    first = await context.append_evidence(
        DIAG, incident_id="INC-TEST-0001", evidence_type="metric", summary="memory", content={"m": 94}, observed_at=now
    )
    second = await context.append_evidence(
        DIAG, incident_id="INC-TEST-0001", evidence_type="metric", summary="memory", content={"m": 94}, observed_at=now
    )
    assert first == second and first.source == "diagnosis-agent"
    ref, content = await context.read_evidence(orchestrator, first.evidence_id)
    assert content == {"m": 94}
    ctx = await context.get_incident_context(orchestrator, "INC-TEST-0001")
    assert [e.evidence_id for e in ctx.evidence_refs] == [first.evidence_id]


async def test_every_write_bumps_version_and_extends_audit_chain(context, orchestrator, store):
    ctx = await open_incident(context)
    await context.record_action(
        orchestrator,
        "INC-TEST-0001",
        CandidateAction(action_id="ACT-1", type="ROLLBACK", source_agent="diagnosis-agent", score=0.91),
    )
    await context.update_incident(orchestrator, "INC-TEST-0001", status="DIAGNOSING")
    after = await context.get_incident_context(orchestrator, "INC-TEST-0001")
    assert after.version == ctx.version + 2
    assert after.status == "DIAGNOSING"
    assert after.candidate_actions[0].action_id == "ACT-1"
    trail = await context.audit_trail(orchestrator, "INC-TEST-0001")
    assert [r["action"] for r in trail] == ["INCIDENT_OPENED", "ACTION_RECORDED", "INCIDENT_UPDATED"]
    assert await context.verify_audit_chain(orchestrator, "INC-TEST-0001")


async def test_tampering_with_audit_is_detected(context, orchestrator, store):
    await open_incident(context)
    await context.update_incident(orchestrator, "INC-TEST-0001", status="DIAGNOSING")
    async with store._engine.begin() as conn:
        await conn.execute(update(t.audit_log).where(t.audit_log.c.seq == 1).values(actor="attacker"))
    assert not await context.verify_audit_chain(orchestrator, "INC-TEST-0001")


async def test_concurrent_writers_keep_one_unbroken_audit_chain(context, orchestrator):
    """Agents and the Orchestrator write the same incident at the same time."""
    import asyncio

    await open_incident(context)
    now = datetime.now(UTC)
    writes = [
        context.append_evidence(
            DIAG,
            incident_id="INC-TEST-0001",
            evidence_type="metric",
            summary=f"m{i}",
            content={"i": i},
            observed_at=now,
        )
        for i in range(15)
    ] + [
        context.record_decision(orchestrator, "INC-TEST-0001", stage="TEST", decision_type=f"D{i}", payload={"i": i})
        for i in range(15)
    ]
    await asyncio.gather(*writes)
    trail = await context.audit_trail(orchestrator, "INC-TEST-0001")
    assert len(trail) == 31
    assert await context.verify_audit_chain(orchestrator, "INC-TEST-0001")


async def test_only_hitl_writes_approvals(context):
    await open_incident(context)
    with pytest.raises(AccessDenied):
        await context.upsert_approval(Principal.orchestrator(), {"incident_id": "INC-TEST-0001"})
