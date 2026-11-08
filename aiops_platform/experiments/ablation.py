"""Ablation: what each safety layer prevents on the hard cases.

    python -m aiops_platform.experiments.ablation --name ablation-v1

Four configurations, the same 18 cases, the rules only:

- full: the shipped system (human approval and the CPU check on).
- no-cpu-check: verification is Agent 1's symptom checks alone.
- no-human-gate: the policy executes without asking a person.
- no-gates: neither.

The operator in the first two is worst-case (approves everything), so a human
gate that approves everything prevents nothing. The gate's protection depends
on the human's judgement, which a simulation cannot provide; the report says so.
"""

import argparse
import asyncio
import copy
import json
import logging
import time
from typing import Any

import aiops_platform.workers.agent1 as agent1
from aiops_platform.experiments import ambiguous as A
from aiops_platform.policy.gate import load_policy

OUT = A.OUT
CONFIGS = ["full", "no-cpu-check", "no-human-gate", "no-gates"]


def no_human_gate_policy() -> dict[str, Any]:
    """The shipped policy with every action pre-authorized, every decision automatic and no trigger."""
    policy = copy.deepcopy(load_policy())
    actions = ("SCALE_REPLICAS", "ROLLBACK", "DEPLOY", "INCREASE_MEMORY", "ROLLING_RESTART", "PROMOTE_MODEL")
    policy["pre_authorized"] = [
        {"environment": env, "action": action}
        for env in ("production", "staging", "test", "local")
        for action in actions
    ]
    policy["thresholds"].update(auto_decision_score=0.0, min_confidence=0.0, max_disagreement=1.0)
    policy["thresholds"]["risk_levels"]["LOW"] = 1.0
    return policy


class PassThroughVerifier:
    """Stands in for ReleaseHealthVerifier: Agent 1's symptom checks, with no CPU check."""

    def __init__(self, inner, cluster) -> None:
        self._inner = inner

    async def verify(self, action_id, context):
        return await self._inner.verify(action_id, context)


def run(config: str) -> list[dict[str, Any]]:
    original = agent1.ReleaseHealthVerifier
    if config in ("no-cpu-check", "no-gates"):
        agent1.ReleaseHealthVerifier = PassThroughVerifier
    try:
        policy = no_human_gate_policy() if config in ("no-human-gate", "no-gates") else None
        return asyncio.run(A.run_all(policy=policy))
    finally:
        agent1.ReleaseHealthVerifier = original


def tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    outcomes = [r["outcome"] for r in rows]
    return {
        "correct": outcomes.count("correct"),
        "wrong change applied and verified as recovered (false recovery)": sum(
            o.startswith("WRONG change executed, verified") for o in outcomes
        ),
        "wrong change applied, not verified": outcomes.count("WRONG change executed"),
        "wrong change applied, caught by verification and reverted": sum(
            o.startswith("WRONG change attempted") for o in outcomes
        ),
        "refused by Agent 4 (nothing applied)": outcomes.count("refused by Agent 4 before execution (nothing applied)"),
        "wrong action shown to a human": sum(o.startswith("wrong action shown") for o in outcomes),
        "control false alarms": outcomes.count("false alarm"),
        "approvals requested": sum(r["approvals_asked"] for r in rows),
    }


def render(name: str, recorded: str, results: dict[str, list[dict[str, Any]]]) -> str:
    keys = list(tally(next(iter(results.values()))).keys())
    lines = [
        f"# Ablation: {name}",
        "",
        f"Recorded {recorded}. Rules only, 18 hard cases, worst-case operator.",
        "",
        "| Measure | " + " | ".join(results) + " |",
        "|---|" + "---|" * len(results),
    ]
    tallies = {config: tally(rows) for config, rows in results.items()}
    for key in keys:
        lines.append(f"| {key} | " + " | ".join(str(tallies[c][key]) for c in results) + " |")
    lines += [
        "",
        "Reading the table: removing the CPU check lets wrong changes be verified as recovered. Removing the",
        "platform's policy gate alone changes nothing here: Agent 4 refuses automated production actions,",
        "because they need a signed human permit (its provenance check, independent of the policy). The human",
        "gate's own protection depends on the operator's judgement, which a simulation cannot measure: the",
        "worst-case operator approves everything.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    recorded = time.strftime("%Y-%m-%d %H:%M")
    results = {config: run(config) for config in CONFIGS}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.name}.json").write_text(
        json.dumps({"results": results, "tallies": {c: tally(r) for c, r in results.items()}}, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT / f"{args.name}.md").write_text(render(args.name, recorded, results), encoding="utf-8")
    print(json.dumps({c: tally(r) for c, r in results.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
