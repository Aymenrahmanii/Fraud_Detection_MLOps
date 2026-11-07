"""Dashboard API: authenticated operators, approval flow through the Orchestrator."""

import httpx
import pytest
import pytest_asyncio

from aiops_platform.dashboard.app import create_app
from aiops_platform.hitl.service import Operator
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import PAYMENT, base_cluster, memory_leak_after_deploy

pytestmark = pytest.mark.asyncio

OPERATORS = {
    "sre-token": Operator(subject="alice@sre", roles=frozenset({"sre"})),
    "viewer-token": Operator(subject="viewer@team", roles=frozenset()),
}


@pytest_asyncio.fixture
async def setup(tmp_path):
    platform = await Platform.in_memory(tmp_path, base_cluster(), watched=[PAYMENT])
    app = create_app(context=platform.context, orchestrator=platform.orchestrator, operators=OPERATORS)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard")
    yield platform, client
    await client.aclose()
    await platform.close()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def test_requires_a_known_operator(setup):
    _, client = setup
    assert (await client.get("/api/incidents")).status_code == 401
    assert (await client.get("/api/incidents", headers=auth("forged"))).status_code == 401
    me = (await client.get("/api/me", headers=auth("sre-token"))).json()
    assert me == {"subject": "alice@sre", "roles": ["sre"]}


async def test_operator_approves_from_the_dashboard(setup):
    platform, client = setup
    memory_leak_after_deploy(platform.cluster)
    [incident] = await platform.watcher.tick()
    await platform.bus.drain(timeout=60)

    [approval] = (await client.get("/api/approvals", headers=auth("sre-token"))).json()
    assert approval["request"]["action"] == "ROLLBACK"
    assert "operation" not in approval["request"]["plan"]  # opaque blob stays server-side
    assert any(op["op"] == "replace" for op in approval["request"]["plan"]["patch"])

    # A viewer has no approval role: the HITL Service refuses, nothing executes.
    denied = await client.post(
        f"/api/approvals/{approval['approval_id']}/resolve",
        headers=auth("viewer-token"),
        json={"decision": "APPROVE", "comment": "looks fine"},
    )
    assert denied.status_code == 403
    assert platform.cluster.mutations == []

    ok = await client.post(
        f"/api/approvals/{approval['approval_id']}/resolve",
        headers=auth("sre-token"),
        json={"decision": "APPROVE", "comment": "rollback, verify 10m"},
    )
    assert ok.status_code == 200 and ok.json()["state"] == "APPROVED"
    assert "approval_token" not in ok.json()
    await platform.bus.drain(timeout=60)

    detail = (await client.get(f"/api/incidents/{incident}", headers=auth("sre-token"))).json()
    assert detail["workflow"]["stage"] == "RESOLVED"
    assert detail["audit_chain_valid"] is True
    # The phase views: Agent 1's detection is exposed, the opaque operation blob is not.
    assert detail["workflow"]["detection"]["payload"]["signals"]
    results = detail["context"]["agent_results"]
    assert any("patch" in r["payload"] for r in results)
    assert all("operation" not in r["payload"] for r in results)
    assert any(
        a["action"] == "APPROVAL_APPROVED" and a["detail"]["approver_id"] == "alice@sre" for a in detail["audit"]
    )

    again = await client.post(
        f"/api/approvals/{approval['approval_id']}/resolve",
        headers=auth("sre-token"),
        json={"decision": "APPROVE", "comment": "twice"},
    )
    assert again.status_code == 409  # already resolved
    assert len(platform.cluster.mutations) == 1
