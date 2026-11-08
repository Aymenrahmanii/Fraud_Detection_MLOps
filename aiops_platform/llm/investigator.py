"""Agent 2's investigation: an LLM checks the rules' diagnosis with read-only tools.

The model can only confirm or challenge the rules' candidate causes. It cannot
raise a confidence, change a probability, choose an action, or look at a
service other than the one in the task. A challenge, or an answer saying the
evidence is insufficient, sends the decision to a human through the existing
"requires additional evidence" path. A model that is unreachable or answers
badly changes nothing; that outcome is recorded, so the cost of the fallback
can be measured.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiops_platform.llm.client import LLMError, ToolCall

INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

SYSTEM = (
    "You review a root-cause diagnosis made by deterministic rules. You are read-only: you cannot change "
    "anything, and you cannot inspect any service other than the one under review. Use the evidence tools to "
    "check the leading hypothesis. Then give your verdict by calling submit_verdict, with the root cause "
    "(one of the candidate codes, or INSUFFICIENT_EVIDENCE) and one sentence citing what the tools showed. "
    "Never invent a code."
)
VERDICT_TOOL = "submit_verdict"


def verdict_declaration(rules: "RulesView") -> dict[str, Any]:
    """The tool the model answers through. Its root cause is an enum of the rules' candidates, so a
    model cannot invent one; the answer is then a structured call rather than free text."""
    return {
        "name": VERDICT_TOOL,
        "description": "Submit your verdict on the leading hypothesis. Call it once, after checking the tools.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "root_cause": {
                    "type": "STRING",
                    "enum": [*rules.candidates, INSUFFICIENT],
                    "description": "The candidate code the tools support, or INSUFFICIENT_EVIDENCE.",
                },
                "reason": {"type": "STRING", "description": "One sentence citing what the tools showed."},
            },
            "required": ["root_cause", "reason"],
        },
    }


@dataclass(frozen=True)
class RulesView:
    primary: str | None
    candidates: tuple[str, ...]
    probabilities: dict[str, float]
    signals: dict[str, Any]


@dataclass
class Investigation:
    model: str
    status: str = "UNAVAILABLE"  # OK | INVALID | UNAVAILABLE
    llm_root_cause: str | None = None
    reason: str = ""
    agrees: bool | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    latency_s: float = 0.0

    @property
    def challenges(self) -> bool:
        """The model disagrees with the rules, or says the evidence is not enough."""
        return self.status == "OK" and self.agrees is False


def _prompt(rules: RulesView) -> str:
    probabilities = ", ".join(f"{code} {p:.2f}" for code, p in rules.probabilities.items()) or "none"
    return (
        f"Rules' leading hypothesis: {rules.primary}. "
        f"Candidates: {', '.join(rules.candidates) or 'none'}. "
        f"Probabilities: {probabilities}. "
        f"Signals at detection: {json.dumps(rules.signals, sort_keys=True, default=str)}. "
        "Check the leading hypothesis with the evidence tools, then submit your verdict."
    )


def _accept(root: Any, reason: Any, rules: RulesView, inv: Investigation) -> None:
    allowed = set(rules.candidates) | {INSUFFICIENT}
    if not isinstance(root, str) or root not in allowed:
        inv.status = "INVALID"
        inv.reason = "the answer did not name an allowed root cause"
        return
    inv.status = "OK"
    inv.llm_root_cause = root
    inv.reason = str(reason or "")[:500]
    inv.agrees = root == rules.primary


def _judge(text: str | None, rules: RulesView, inv: Investigation) -> None:
    """A plain-text answer, for a model that replies in JSON instead of calling the verdict tool."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        _accept(None, None, rules, inv)
        return
    _accept(data.get("root_cause"), data.get("reason"), rules, inv)


class Investigator:
    def __init__(
        self,
        client: Any,
        tools: dict[str, Callable[[str, str], dict[str, Any]]],
        declarations: Sequence[dict[str, Any]],
        *,
        max_tool_calls: int = 3,
        max_model_calls: int = 4,
        deadline_s: float = 120.0,
    ) -> None:
        self._client = client
        self._tools = tools
        self._declarations = list(declarations)
        self._max_tool_calls = max_tool_calls
        self._max_model_calls = max_model_calls
        self._deadline_s = deadline_s

    async def investigate(self, *, service_id: str, namespace: str, rules: RulesView) -> Investigation:
        inv = Investigation(model=self._client.model_name)
        started = time.perf_counter()
        deadline = started + self._deadline_s
        contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": _prompt(rules)}]}]
        declarations = [*self._declarations, verdict_declaration(rules)]
        try:
            while True:
                if inv.model_calls >= self._max_model_calls:
                    inv.reason = "model-call budget exhausted without an answer"
                    break
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    inv.reason = "deadline exceeded"
                    break
                inv.model_calls += 1
                reply = await asyncio.wait_for(
                    self._client.generate(system=SYSTEM, contents=contents, tools=declarations),
                    timeout=remaining,
                )
                contents.append(reply.raw)
                verdict_call = next((call for call in reply.tool_calls if call.name == VERDICT_TOOL), None)
                if verdict_call is not None:
                    _accept(verdict_call.args.get("root_cause"), verdict_call.args.get("reason"), rules, inv)
                    break
                if reply.tool_calls:
                    responses = [self._run_tool(call, service_id, namespace, inv) for call in reply.tool_calls]
                    contents.append({"role": "user", "parts": responses})
                    continue
                _judge(reply.text, rules, inv)
                break
        except TimeoutError:
            inv.status, inv.reason = "UNAVAILABLE", "deadline exceeded"
        except LLMError as exc:
            inv.status, inv.reason = "UNAVAILABLE", str(exc)
        inv.latency_s = time.perf_counter() - started
        return inv

    def _run_tool(self, call: ToolCall, service_id: str, namespace: str, inv: Investigation) -> dict[str, Any]:
        """Runs one read-only tool for the task's service. Arguments from the model are ignored."""
        if len(inv.tool_calls) >= self._max_tool_calls:
            result: dict[str, Any] = {"error": "tool-call budget exhausted"}
        elif call.name not in self._tools:
            result = {"error": f"unknown tool {call.name!r}"}
        else:
            try:
                result = self._tools[call.name](service_id, namespace)
            except Exception as exc:  # a failing tool is information for the model, not a crash
                result = {"error": f"tool failed: {type(exc).__name__}"}
            inv.tool_calls.append({"name": call.name, "model_args": call.args, "result": result})
        response: dict[str, Any] = {"name": call.name, "response": {"result": result}}
        if call.id is not None:
            response["id"] = call.id  # providers that number their calls need the result to carry the number
        return {"functionResponse": response}


def apply_review(payload: dict[str, Any], inv: Investigation) -> dict[str, Any]:
    """The rules' payload with the investigation recorded. Only a challenge changes a decision field,
    and it can only set the existing "requires additional evidence" flag. Hypotheses, probabilities,
    confidence and evidence strength are never touched."""
    out = dict(payload)
    out["llm_investigation"] = {
        "model": inv.model,
        "status": inv.status,
        "llm_root_cause": inv.llm_root_cause,
        "agrees": inv.agrees,
        "reason": inv.reason,
        "tool_calls": [call["name"] for call in inv.tool_calls],
        "model_calls": inv.model_calls,
        "latency_s": round(inv.latency_s, 3),
    }
    if inv.challenges:
        out["requires_additional_evidence"] = True
        out["limitations"] = [
            *(out.get("limitations") or []),
            f"LLM investigation challenges the rules: {inv.llm_root_cause}. {inv.reason}",
        ]
    return out
