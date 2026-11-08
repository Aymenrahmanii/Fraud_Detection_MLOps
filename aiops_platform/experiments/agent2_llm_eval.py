"""Agent 2 with and without the LLM review, on the baseline's scenarios.

    GEMINI_API_KEY=... python -m aiops_platform.experiments.agent2_llm_eval --name agent2-llm-v1

Each scenario runs twice on an identical simulated cluster: once with the rules
alone, once with the rules plus Gemini's review. The report compares the two,
and the JSON file keeps every run.
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from aiops_platform.context.service import Principal
from aiops_platform.contracts.enums import Environment
from aiops_platform.hitl.service import Operator
from aiops_platform.llm.client import GeminiClient
from aiops_platform.llm.investigator import Investigator
from aiops_platform.llm.sim_tools import DECLARATIONS, make_sim_tools
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster, cpu_load_spike, memory_leak_after_deploy

SRE = Operator(subject="eval@sre", roles=frozenset({"sre"}))
ORCH = Principal.orchestrator()
OUT = Path(__file__).with_name("baselines")
SCENARIOS = [("leak", 7), ("leak", 9), ("leak", 12), ("cpu", 550), ("cpu", 640), ("cpu", 800)]


async def review_of(platform: Platform, incident: str) -> dict[str, Any]:
    """The LLM review recorded for this incident as evidence, or OFF when the review was not run."""
    context = await platform.context.get_incident_context(ORCH, incident)
    for ref in context.evidence_refs:
        if ref.evidence_id.startswith("EVD-llm_review-"):
            _, content = await platform.context.read_evidence(ORCH, ref.evidence_id)
            return content
    return {"status": "OFF"}


async def run_case(scenario: str, severity: float, *, investigator, workdir: Path) -> list[dict[str, Any]]:
    environment = Environment.STAGING if scenario == "cpu" else Environment.PRODUCTION
    cluster = base_cluster()
    watched = [CHECKOUT] if scenario == "cpu" else [PAYMENT, CHECKOUT]
    platform = await Platform.in_memory(
        workdir,
        cluster,
        watched=watched,
        environment=environment,
        agent2_investigator=investigator(cluster) if investigator else None,
    )
    try:
        if scenario == "cpu":
            cpu_load_spike(cluster, rps=severity)
        else:
            memory_leak_after_deploy(cluster, minutes=severity)
        opened = await platform.watcher.tick()
        await platform.bus.drain(timeout=120)
        asked = 0
        for approval in await platform.pending_approvals():
            asked += 1
            await platform.orchestrator.resolve_approval(
                approval["approval_id"], decision="APPROVE", operator=SRE, comment="agent2 evaluation"
            )
            await platform.bus.drain(timeout=120)
        rows = []
        for incident in opened:
            status = await platform.orchestrator.status(incident)
            review = await review_of(platform, incident)
            consensus = status.get("consensus") or {}
            rows.append(
                {
                    "scenario": scenario,
                    "severity": severity,
                    "stage": status.get("stage"),
                    "decision": (status.get("policy") or {}).get("decision"),
                    "selected": (status.get("selected") or {}).get("type"),
                    "root_cause": consensus.get("primary_root_cause"),
                    "agreement": consensus.get("agreement"),
                    "approvals_asked": asked,
                    "llm_status": review.get("status"),
                    "llm_root_cause": review.get("llm_root_cause"),
                    "llm_reason": review.get("reason"),
                    "llm_agrees": review.get("agrees"),
                    "challenged": review.get("status") == "OK" and review.get("agrees") is False,
                    "tool_calls": len(review.get("tool_calls", [])),
                    "model_calls": review.get("model_calls", 0),
                    "llm_latency_s": review.get("latency_s"),
                    "resolved": status.get("stage") == "RESOLVED",
                    "unsafe": int(len(cluster.mutations) > 0 and status.get("stage") != "RESOLVED"),
                }
            )
        return rows
    finally:
        await platform.close()


def summarize(runs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    rules, llm = runs["rules"], runs["llm"]
    latencies = [r["llm_latency_s"] for r in llm if r["llm_latency_s"] is not None]
    statuses = {s: sum(r["llm_status"] == s for r in llm) for s in ("OK", "INVALID", "UNAVAILABLE")}
    same_outcome = sum(
        a["stage"] == b["stage"] and a["decision"] == b["decision"] for a, b in zip(rules, llm, strict=True)
    )
    return {
        "runs": len(llm),
        "llm_status": statuses,
        "agrees_with_rules": sum(r["llm_agrees"] is True for r in llm),
        "challenges": sum(r["challenged"] for r in llm),
        "same_outcome_as_rules": same_outcome,
        "rules_resolved": sum(r["resolved"] for r in rules),
        "llm_resolved": sum(r["resolved"] for r in llm),
        "unsafe_rules": sum(r["unsafe"] for r in rules),
        "unsafe_llm": sum(r["unsafe"] for r in llm),
        "rate_limited": sum(r["llm_reason"] == "HTTP 429" for r in llm),
        "tool_calls_mean": round(statistics.mean(r["tool_calls"] for r in llm), 2) if llm else None,
        "model_calls_mean": round(statistics.mean(r["model_calls"] for r in llm), 2) if llm else None,
        "llm_latency_p50_s": round(statistics.median(latencies), 3) if latencies else None,
        "llm_latency_max_s": round(max(latencies), 3) if latencies else None,
    }


def render(name: str, model: str, data: dict[str, Any]) -> str:
    s = data["summary"]
    lines = [
        f"# Agent 2 with an LLM review: {name}",
        "",
        f"Model: `{model}` (Google Gemini). Recorded {data['recorded_at']}.",
        "Same six scenarios and seed as the deterministic baseline (`deterministic-v1`).",
        "",
        "## Summary",
        "",
        f"- Investigations: {s['runs']}. Model answered: {s['llm_status']}.",
        f"- Agreed with the rules: {s['agrees_with_rules']}. Challenged the rules (sent to a human): {s['challenges']}.",
        f"- Same stage and decision as the rules: {s['same_outcome_as_rules']} of {s['runs']}.",
        f"- Resolved: rules {s['rules_resolved']} of {s['runs']}, with the review {s['llm_resolved']} of {s['runs']}.",
        f"- Unsafe actions: rules {s['unsafe_rules']}, with the review {s['unsafe_llm']}.",
        f"- Tool calls per investigation (mean): {s['tool_calls_mean']}. Model calls (mean): {s['model_calls_mean']}.",
        f"- Model latency per investigation: median {s['llm_latency_p50_s']} s, slowest {s['llm_latency_max_s']} s.",
        "",
        "## Runs",
        "",
        "| Scenario | Severity | Rules: stage / decision | Review: answer | Agrees | Challenged | Review: stage / decision | Tools | Model calls | Latency (s) | Note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rules, llm in zip(data["runs_rules"], data["runs_llm"], strict=True):
        note = (llm.get("llm_reason") or "").replace("|", "/")[:90]
        lines.append(
            f"| {rules['scenario']} | {rules['severity']} | {rules['stage']} / {rules['decision']} | "
            f"{llm['llm_status']} {llm['llm_root_cause'] or ''} | {llm['llm_agrees']} | {llm['challenged']} | "
            f"{llm['stage']} / {llm['decision']} | {llm['tool_calls']} | {llm['model_calls']} | {llm['llm_latency_s']} | {note} |"
        )
    lines += [
        "",
        "## Limits",
        "",
        "- Six scenarios on a simulated cluster, where the faults are clear. Few cases are ambiguous, so the review has little room to disagree.",
        f"- {s['rate_limited']} of {s['runs']} reviews were refused with HTTP 429 (the key's quota was used up). Those runs fell back to the rules; they say nothing about the model.",
        "- The review can only add caution. It cannot show whether it would catch a wrong diagnosis, which needs scenarios where the rules are wrong.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", default=os.environ.get("AIOPS_AGENT2_MODEL", "gemini-3.8-flash"))
    parser.add_argument("--pause", type=float, default=25.0, help="seconds between model-backed runs (free-tier quota)")
    args = parser.parse_args(argv)
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise SystemExit("set GEMINI_API_KEY in the environment (never in a file)")
    logging.disable(logging.CRITICAL)
    started = time.time()

    async def everything() -> dict[str, list[dict[str, Any]]]:
        client = GeminiClient(model=args.model, api_key=key, timeout_s=30)
        runs: dict[str, list[dict[str, Any]]] = {"rules": [], "llm": []}
        try:
            with tempfile.TemporaryDirectory(prefix="agent2-eval-", ignore_cleanup_errors=True) as raw:
                tmp = Path(raw)
                for i, (scenario, severity) in enumerate(SCENARIOS):
                    runs["rules"] += await run_case(scenario, severity, investigator=None, workdir=tmp / f"r{i}")
                    # The free tier allows a few requests per minute; pause so each review gets its quota.
                    await asyncio.sleep(args.pause)
                    runs["llm"] += await run_case(
                        scenario,
                        severity,
                        investigator=lambda cluster: Investigator(client, make_sim_tools(cluster), DECLARATIONS),
                        workdir=tmp / f"l{i}",
                    )
        finally:
            await client.aclose()
        return runs

    runs = asyncio.run(everything())
    data = {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M"),
        "model": args.model,
        "runs_rules": runs["rules"],
        "runs_llm": runs["llm"],
        "summary": summarize(runs),
        "wall_seconds": round(time.time() - started, 1),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (OUT / f"{args.name}.md").write_text(render(args.name, args.model, data), encoding="utf-8")
    print(json.dumps(data["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
