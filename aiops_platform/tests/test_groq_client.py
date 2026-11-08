"""The Groq client: request shape, tool-call matching, and errors. A mock transport stands in for the API."""

import json

import httpx
import pytest

from aiops_platform.llm.client import GroqClient, LLMError
from aiops_platform.llm.investigator import Investigator, RulesView
from aiops_platform.llm.sim_tools import DECLARATIONS

pytestmark = pytest.mark.asyncio

RULES = RulesView(
    primary="MEMORY_LEAK_POST_DEPLOY",
    candidates=("MEMORY_LEAK_POST_DEPLOY",),
    probabilities={"MEMORY_LEAK_POST_DEPLOY": 0.7},
    signals={"memory_pct": 81.3},
)


def chat(content=None, tool_calls=None, status=200):
    if status != 200:
        return httpx.Response(status, json={"error": "secret body must not leak"})
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def call(call_id, name, arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def make_client(handler):
    return GroqClient(model="openai/gpt-oss-120b", api_key="test-key", transport=httpx.MockTransport(handler))


async def test_the_request_carries_the_system_prompt_tools_and_user_agent():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        return chat(content="ok")

    client = make_client(handler)
    await client.generate(system="SYSTEM", contents=[{"role": "user", "parts": [{"text": "hi"}]}], tools=DECLARATIONS)
    body = seen["body"]
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_current_signals"
    assert body["tools"][0]["function"]["parameters"]["type"] == "object"
    assert body["tool_choice"] == "auto"
    assert seen["headers"]["user-agent"] == "aiops-platform/1.0"
    assert seen["headers"]["authorization"] == "Bearer test-key"
    assert "test-key" not in seen["url"]  # the key is in a header, never in the URL
    await client.aclose()


async def test_a_tool_result_is_matched_to_its_call_by_id():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return chat(tool_calls=[call("call_abc", "get_current_signals")])
        return chat(content='{"root_cause": "MEMORY_LEAK_POST_DEPLOY", "reason": "memory climbs"}')

    inv = await Investigator(
        make_client(handler), {"get_current_signals": lambda s, n: {"memory_pct": 81.3}}, DECLARATIONS
    ).investigate(service_id="payment-service", namespace="prod", rules=RULES)
    assert inv.status == "OK" and inv.agrees is True
    assert [c["name"] for c in inv.tool_calls] == ["get_current_signals"]
    second = requests[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    tool = next(m for m in second if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_abc"
    assert tool["tool_call_id"] == "call_abc"
    assert json.loads(tool["content"]) == {"result": {"memory_pct": 81.3}}


async def test_bad_tool_arguments_become_empty_arguments_not_an_error():
    def handler(request):
        return chat(tool_calls=[call("c1", "get_current_signals", arguments="{not json")])

    client = make_client(handler)
    reply = await client.generate(system="s", contents=[{"role": "user", "parts": [{"text": "x"}]}], tools=DECLARATIONS)
    assert reply.tool_calls[0].args == {}
    await client.aclose()


async def test_an_http_error_is_reported_by_status_only():
    client = make_client(lambda request: chat(status=500))
    with pytest.raises(LLMError, match="HTTP 500") as info:
        await client.generate(system="s", contents=[{"role": "user", "parts": [{"text": "x"}]}], tools=[])
    assert "secret body" not in str(info.value)
    await client.aclose()


async def test_a_missing_key_is_refused_at_start_up():
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqClient(model="openai/gpt-oss-120b", api_key="")


async def test_the_verdict_is_a_structured_call_with_the_candidates_as_an_enum(monkeypatch):
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["tools"] = body["tools"]
        return chat(
            tool_calls=[
                call(
                    "v1", "submit_verdict", arguments='{"root_cause": "MEMORY_LEAK_POST_DEPLOY", "reason": "climbing"}'
                )
            ]
        )

    inv = await Investigator(make_client(handler), {}, DECLARATIONS).investigate(
        service_id="payment-service", namespace="prod", rules=RULES
    )
    assert inv.status == "OK" and inv.agrees is True and inv.model_calls == 1
    verdict_tool = next(t for t in seen["tools"] if t["function"]["name"] == "submit_verdict")
    properties = verdict_tool["function"]["parameters"]["properties"]
    assert verdict_tool["function"]["parameters"]["type"] == "object"
    assert properties["root_cause"]["type"] == "string"
    assert properties["root_cause"]["enum"] == ["MEMORY_LEAK_POST_DEPLOY", "INSUFFICIENT_EVIDENCE"]


async def test_a_rate_limit_is_waited_out_then_answered(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("aiops_platform.llm.client.asyncio.sleep", fake_sleep)
    answers = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate limited"}),
            chat(content='{"root_cause": "MEMORY_LEAK_POST_DEPLOY", "reason": "after the wait"}'),
        ]
    )
    inv = await Investigator(make_client(lambda request: next(answers)), {}, DECLARATIONS).investigate(
        service_id="payment-service", namespace="prod", rules=RULES
    )
    assert waits == [2.0]
    assert inv.status == "OK" and inv.agrees is True


async def test_a_rate_limit_that_persists_is_reported_after_the_retries(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("aiops_platform.llm.client.asyncio.sleep", fake_sleep)
    client = make_client(lambda request: httpx.Response(429, json={"error": "limit"}))
    with pytest.raises(LLMError, match="HTTP 429"):
        await client.generate(system="s", contents=[{"role": "user", "parts": [{"text": "x"}]}], tools=[])
    await client.aclose()
