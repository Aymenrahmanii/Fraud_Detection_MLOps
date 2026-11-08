"""Agent 2's LLM review: what the model may change, and what it may not.

The model is scripted, so these tests need no network and no key.
"""

import asyncio
import re

import pytest
import pytest_asyncio

from aiops_platform.context.service import Principal
from aiops_platform.llm.client import LLMError, Reply, ToolCall
from aiops_platform.llm.investigator import INSUFFICIENT, Investigator, RulesView, apply_review
from aiops_platform.llm.sim_tools import DECLARATIONS, make_sim_tools
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster, memory_leak_after_deploy

pytestmark = pytest.mark.asyncio
ORCH = Principal.orchestrator()

RULES = RulesView(
    primary="MEMORY_LEAK_POST_DEPLOY",
    candidates=("MEMORY_LEAK_POST_DEPLOY", "CONFIG_ERROR_CRASHLOOP"),
    probabilities={"MEMORY_LEAK_POST_DEPLOY": 0.7, "CONFIG_ERROR_CRASHLOOP": 0.2},
    signals={"memory_pct": 81.3},
)


def tool(name: str, args: dict | None = None) -> Reply:
    call = {"name": name, "args": args or {}}
    return Reply(text=None, tool_calls=(ToolCall(**call),), raw={"role": "model", "parts": [{"functionCall": call}]})


def answer(text: str) -> Reply:
    return Reply(text=text, tool_calls=(), raw={"role": "model", "parts": [{"text": text}]})


def verdict(root: str, reason: str = "checked the tools") -> Reply:
    return answer(f'{{"root_cause": "{root}", "reason": "{reason}"}}')


def agree(contents) -> Reply:
    """A model that confirms whatever the rules said, read from the prompt."""
    text = contents[0]["parts"][0]["text"]
    return verdict(re.search(r"leading hypothesis: ([A-Z_]+)\.", text).group(1))


class FakeClient:
    model_name = "fake-model"

    def __init__(self, script=(), default=agree):
        self._script = list(script)
        self._default = default

    async def generate(self, *, system, contents, tools):
        item = self._script.pop(0) if self._script else self._default(contents)
        if isinstance(item, Exception):
            raise item
        return item


class SlowClient(FakeClient):
    async def generate(self, *, system, contents, tools):
        await asyncio.sleep(10)


def recorder(calls: list):
    def signals(service, namespace):
        calls.append((service, namespace))
        return {"memory_pct": 81.3}

    return {"get_current_signals": signals}


def hypotheses_payload() -> dict:
    return {
        "primary_root_cause": "MEMORY_LEAK_POST_DEPLOY",
        "hypotheses": [
            {"code": "MEMORY_LEAK_POST_DEPLOY", "probability": 0.7},
            {"code": "CONFIG_ERROR_CRASHLOOP", "probability": 0.2},
        ],
        "requires_additional_evidence": False,
        "limitations": [],
        "evidence_strength": 0.8,
    }


async def investigate(client, tools, *, max_tool_calls=3, max_model_calls=4, deadline_s=60.0):
    investigator = Investigator(
        client,
        tools,
        DECLARATIONS,
        max_tool_calls=max_tool_calls,
        max_model_calls=max_model_calls,
        deadline_s=deadline_s,
    )
    return await investigator.investigate(service_id="payment-service", namespace="prod", rules=RULES)


# ---- what the model may change ----------------------------------------------------


async def test_agreement_leaves_every_decision_field_unchanged():
    calls: list = []
    inv = await investigate(
        FakeClient([tool("get_current_signals"), verdict("MEMORY_LEAK_POST_DEPLOY")]), recorder(calls)
    )
    payload = hypotheses_payload()
    out = apply_review(payload, inv)
    assert inv.status == "OK" and inv.agrees is True and not inv.challenges
    assert calls == [("payment-service", "prod")]
    assert out["requires_additional_evidence"] is False
    assert out["hypotheses"] == payload["hypotheses"]
    assert out["evidence_strength"] == 0.8
    assert out["llm_investigation"]["tool_calls"] == ["get_current_signals"]


async def test_challenge_sends_the_decision_to_a_human_without_touching_probabilities():
    inv = await investigate(
        FakeClient([verdict("CONFIG_ERROR_CRASHLOOP", "restarts show a config fault")]), recorder([])
    )
    out = apply_review(hypotheses_payload(), inv)
    assert inv.challenges
    assert out["requires_additional_evidence"] is True
    assert any("challenges the rules" in line for line in out["limitations"])
    assert out["hypotheses"] == hypotheses_payload()["hypotheses"]
    assert out["evidence_strength"] == 0.8


async def test_insufficient_evidence_counts_as_a_challenge():
    inv = await investigate(FakeClient([verdict(INSUFFICIENT)]), recorder([]))
    assert inv.challenges and inv.llm_root_cause == INSUFFICIENT


@pytest.mark.parametrize(
    "reply",
    [answer("I think it is a leak, probably."), verdict("MADE_UP_CAUSE")],
    ids=["not-json", "invented-code"],
)
async def test_an_unclear_answer_changes_nothing(reply):
    inv = await investigate(FakeClient([reply]), recorder([]))
    out = apply_review(hypotheses_payload(), inv)
    assert inv.status == "INVALID" and not inv.challenges
    assert out["requires_additional_evidence"] is False


async def test_an_unreachable_model_changes_nothing_and_is_recorded():
    inv = await investigate(FakeClient([LLMError("HTTP 503")]), recorder([]))
    out = apply_review(hypotheses_payload(), inv)
    assert inv.status == "UNAVAILABLE" and inv.reason == "HTTP 503"
    assert out["requires_additional_evidence"] is False
    assert out["llm_investigation"]["status"] == "UNAVAILABLE"


# ---- what the model may look at, and how far ----------------------------------------


async def test_the_model_cannot_choose_another_service():
    calls: list = []
    await investigate(
        FakeClient([tool("get_current_signals", {"service": "checkout-api"}), verdict("MEMORY_LEAK_POST_DEPLOY")]),
        recorder(calls),
    )
    assert calls == [("payment-service", "prod")]


async def test_an_unknown_tool_is_refused_and_nothing_runs():
    calls: list = []
    inv = await investigate(FakeClient([tool("delete_pods"), verdict("MEMORY_LEAK_POST_DEPLOY")]), recorder(calls))
    assert calls == [] and inv.status == "OK" and inv.tool_calls == []


async def test_the_tool_budget_is_enforced():
    calls: list = []
    script = [tool("get_current_signals")] * 3 + [verdict("MEMORY_LEAK_POST_DEPLOY")]
    inv = await investigate(FakeClient(script), recorder(calls), max_tool_calls=2)
    assert len(calls) == 2 and len(inv.tool_calls) == 2
    assert inv.status == "OK"


async def test_the_model_call_budget_ends_an_endless_investigation():
    inv = await investigate(
        FakeClient([tool("get_current_signals")] * 10), recorder([]), max_tool_calls=99, max_model_calls=4
    )
    assert inv.status == "UNAVAILABLE" and "budget" in inv.reason
    assert inv.model_calls == 4


async def test_the_deadline_ends_a_stuck_model():
    inv = await investigate(SlowClient(), recorder([]), deadline_s=0.2)
    assert inv.status == "UNAVAILABLE" and inv.reason == "deadline exceeded"


# ---- end to end, through the real loop ----------------------------------------------


@pytest_asyncio.fixture
async def make_platform(tmp_path):
    platforms = []

    async def build(name: str, client):
        cluster = base_cluster()
        investigator = None if client is None else Investigator(client, make_sim_tools(cluster), DECLARATIONS)
        platform = await Platform.in_memory(
            tmp_path / name, cluster, watched=[PAYMENT, CHECKOUT], agent2_investigator=investigator
        )
        platforms.append(platform)
        memory_leak_after_deploy(cluster)
        [incident] = await platform.watcher.tick()
        await platform.bus.drain(timeout=60)
        return platform, incident

    yield build
    for platform in platforms:
        await platform.close()


async def test_a_confirming_model_does_not_change_the_workflow(make_platform):
    rules_only, baseline = await make_platform("rules", None)
    platform, incident = await make_platform("llm", FakeClient())
    with_model = await platform.orchestrator.status(incident)
    without = await rules_only.orchestrator.status(baseline)
    assert with_model["stage"] == without["stage"] == "WAITING_FOR_APPROVAL"
    assert with_model["selected"]["type"] == without["selected"]["type"] == "ROLLBACK"
    assert with_model["consensus"]["agreement"] == without["consensus"]["agreement"]
    assert with_model["policy"]["hitl_triggers"] == without["policy"]["hitl_triggers"]


async def test_a_challenge_is_recorded_and_raises_a_disagreement_trigger(make_platform):
    model = FakeClient([tool("get_current_signals")], default=lambda c: verdict(INSUFFICIENT))
    platform, incident = await make_platform("challenge", model)
    status = await platform.orchestrator.status(incident)
    assert status["consensus"]["agreement"] == 0.45
    assert any("high specialist disagreement" in t for t in status["policy"]["hitl_triggers"])
    context = await platform.context.get_incident_context(ORCH, incident)
    assert any(ref.evidence_id.startswith("EVD-llm_tool:get_current_signals") for ref in context.evidence_refs)
