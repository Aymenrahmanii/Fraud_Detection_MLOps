"""Hard cases: incidents that look like one thing and are another. Ground truth is fixed in advance.

Each family has a symptom that a fixed rule can mislead:

- leak: memory climbs after a deploy. Truth: roll back. (Control for the other families.)
- cpu_burn: a release burns CPU at any traffic level. Truth: roll back. A scale-up
  looks like the obvious fix and only hides the release problem.
- load_memory: a traffic spike pushes memory and CPU up after a deploy. Truth: scale.
  A rollback looks plausible because a deploy came first, and does nothing.
- traffic: a traffic spike on a service with no recent deploy. Truth: scale.
- control: nothing happens. Truth: no incident.

Two operators are modelled:

- worst: approves every request. This is what happens if the operator trusts the proposal.
- informed: rejects a request whose reasons include a specialist disagreement, and
  approves the rest. A review that challenges the rules changes what this operator does.

Rules-only runs need no model. With `--llm groq` the Agent 2 review runs on the
given model, once per operator.

    python -m aiops_platform.experiments.ambiguous --name ambiguous-rules-v1
    GROQ_API_KEY=... python -m aiops_platform.experiments.ambiguous --name ambiguous-groq-v1 --llm groq --model openai/gpt-oss-120b
"""

import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_platform.experiments.agent2_llm_eval import review_of
from aiops_platform.hitl.service import Operator
from aiops_platform.llm.client import GeminiClient, GroqClient
from aiops_platform.llm.investigator import Investigator
from aiops_platform.llm.sim_tools import DECLARATIONS, make_sim_tools
from aiops_platform.runtime import Platform
from aiops_platform.sim.cluster import Release, SimulatedCluster, digest_image
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT

SRE = Operator(subject="ambiguous@sre", roles=frozenset({"sre"}))
OUT = Path(__file__).with_name("baselines")
DISAGREEMENT = "high specialist disagreement"


def service_cluster(payment_fault: str | None) -> SimulatedCluster:
    """payment-service running v2.6, with a v2.7 build that carries `payment_fault`; checkout-api healthy."""
    cluster = SimulatedCluster()
    repo = "registry.local/payment-service"
    faults = frozenset({payment_fault}) if payment_fault else frozenset()
    cluster.add_service(
        "payment-service",
        "prod",
        [Release("v2.6", digest_image(repo, "v2.6")), Release("v2.7", digest_image(repo, "v2.7"), faults)],
        current="v2.6",
        replicas=4,
    )
    repo = "registry.local/checkout-api"
    cluster.add_service(
        "checkout-api",
        "prod",
        [Release("v1.3", digest_image(repo, "v1.3")), Release("v1.4", digest_image(repo, "v1.4"))],
        current="v1.4",
        replicas=3,
        rps=250,
    )
    return cluster


@dataclass(frozen=True)
class Case:
    family: str
    variant: str
    truth: str | None  # the one action that is right, or None for "nothing to do"
    payment_fault: str | None
    inject: Callable[[SimulatedCluster], None]


def _deploy_v27(cluster: SimulatedCluster) -> None:
    cluster.release_version("payment-service", "prod", "v2.7")


def cases() -> list[Case]:
    out: list[Case] = []
    for minutes in (8, 9, 10, 11):
        out.append(
            Case(
                "leak",
                f"{minutes} min after deploy",
                "ROLLBACK",
                "memory_leak",
                lambda c, m=minutes: (_deploy_v27(c), c.advance(m)),
            )
        )
    for rps in (260, 300, 340, 380):
        out.append(
            Case(
                "cpu_burn",
                f"{rps} rps",
                "ROLLBACK",
                "cpu_burn",
                lambda c, r=rps: (_deploy_v27(c), c.set_load("payment-service", "prod", r), c.advance(9)),
            )
        )
    for rps in (680, 720, 760, 800):
        out.append(
            Case(
                "load_memory",
                f"{rps} rps after deploy",
                "SCALE_REPLICAS",
                "load_memory",
                lambda c, r=rps: (_deploy_v27(c), c.set_load("payment-service", "prod", r), c.advance(9)),
            )
        )
    for rps in (600, 640, 680, 720):
        out.append(
            Case(
                "traffic",
                f"{rps} rps, no deploy",
                "SCALE_REPLICAS",
                None,
                lambda c, r=rps: (c.set_load("checkout-api", "prod", r), c.advance(9)),
            )
        )
    out.append(Case("control", "steady traffic, 9 min", None, None, lambda c: c.advance(9)))
    out.append(Case("control", "healthy deploy, 9 min", None, None, lambda c: (_deploy_v27(c), c.advance(9))))
    return out


def snapshot(cluster: SimulatedCluster) -> dict[str, tuple[str, int, str]]:
    return {
        name: (dep.image, dep.replicas, dep.version)
        for name, dep in ((s, cluster.deployment(s, "prod")) for s in ("payment-service", "checkout-api"))
    }


def executed(before: dict, after: dict) -> list[str]:
    actions = []
    for name, (image, replicas, version) in after.items():
        old_image, old_replicas, old_version = before[name]
        if image != old_image:
            actions.append("ROLLBACK" if _is_older(version, old_version) else "DEPLOY")
        if replicas != old_replicas:
            actions.append("SCALE_REPLICAS")
    return actions


def _is_older(version: str, than: str) -> bool:
    return version < than  # v2.6 < v2.7, the only release order used here


WRONG_PROPOSAL_TYPES = {"ROLLBACK", "SCALE_REPLICAS"}


def verdict(case: Case, opened: bool, stage: str | None, proposed: str | None, run: list[str]) -> str:
    if case.truth is None:
        return "quiet (correct)" if not opened else "false alarm"
    if not opened:
        return "missed (no incident)"
    if any(action != case.truth for action in run):
        # The platform's own verdict, when it says RESOLVED after a wrong change, is a false recovery.
        if stage == "RESOLVED":
            return "WRONG change executed, reported RESOLVED (false recovery)"
        return "WRONG change executed"
    if case.truth in run:
        return "correct"
    if proposed in WRONG_PROPOSAL_TYPES and proposed != case.truth:
        return "wrong action shown to a human (nothing executed)"
    if stage == "ESCALATED":
        return "escalated to a human (nothing executed)"
    return f"no change (stage {stage})"


def operator_decision(operator: str, approval: dict[str, Any]) -> tuple[str, str]:
    """The operator's choice for one approval request: (decision, comment)."""
    if operator == "worst":
        return "APPROVE", "worst-case operator"
    reasons = (approval.get("request_json") or {}).get("hitl_triggers") or []
    if any(DISAGREEMENT in reason for reason in reasons):
        return "REJECT", "rejected: the diagnosis is flagged as disputed"
    return "APPROVE", "approved: no disagreement flagged"


async def run_case(case: Case, *, client, operator: str, workdir: Path) -> dict[str, Any]:
    cluster = service_cluster(case.payment_fault)
    investigator = Investigator(client, make_sim_tools(cluster), DECLARATIONS) if client is not None else None
    platform = await Platform.in_memory(workdir, cluster, watched=[PAYMENT, CHECKOUT], agent2_investigator=investigator)
    try:
        case.inject(cluster)
        opened = await platform.watcher.tick()
        await platform.bus.drain(timeout=120)
        before = snapshot(cluster)
        proposed = stage = None
        for incident in opened[:1]:
            status = await platform.orchestrator.status(incident)
            proposed = (status.get("selected") or {}).get("type")
        approvals = await platform.pending_approvals()
        for approval in approvals:
            decision, comment = operator_decision(operator, approval)
            await platform.orchestrator.resolve_approval(
                approval["approval_id"], decision=decision, operator=SRE, comment=comment
            )
            await platform.bus.drain(timeout=120)
        if opened:
            stage = (await platform.orchestrator.status(opened[0])).get("stage")
        run = executed(before, snapshot(cluster))
        review = await review_of(platform, opened[0]) if opened else {"status": "OFF"}
        return {
            "family": case.family,
            "variant": case.variant,
            "truth": case.truth,
            "opened": len(opened),
            "proposed": proposed,
            "approvals_asked": len(approvals),
            "executed": run,
            "stage": stage,
            "outcome": verdict(case, bool(opened), stage, proposed, run),
            "review_status": review.get("status"),
            "review_root_cause": review.get("llm_root_cause"),
            "review_agrees": review.get("agrees"),
            "review_reason": review.get("reason"),
            "review_tool_calls": len(review.get("tool_calls", [])),
            "review_model_calls": review.get("model_calls", 0),
            "review_latency_s": review.get("latency_s"),
        }
    finally:
        await platform.close()


async def run_all(*, client=None, operator: str = "worst", pause: float = 0.0) -> list[dict[str, Any]]:
    rows = []
    with tempfile.TemporaryDirectory(prefix="ambiguous-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        for i, case in enumerate(cases()):
            if client is not None and pause and i:
                await asyncio.sleep(pause)  # the provider's per-minute quota
            rows.append(await run_case(case, client=client, operator=operator, workdir=tmp / f"case-{i}"))
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row["family"], {})
        bucket[row["outcome"]] = bucket.get(row["outcome"], 0) + 1
    return summary


def review_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [r for r in rows if r.get("review_status") not in (None, "OFF")]
    return {
        "reviews": len(reviewed),
        "answered": sum(r["review_status"] == "OK" for r in reviewed),
        "challenged": sum(r["review_agrees"] is False for r in reviewed),
        "unavailable": sum(r["review_status"] == "UNAVAILABLE" for r in reviewed),
        "invalid": sum(r["review_status"] == "INVALID" for r in reviewed),
    }


def render(
    name: str,
    recorded: str,
    rules: list[dict[str, Any]],
    runs: dict[str, list[dict[str, Any]]],
    model: str | None,
) -> str:
    lines = [
        f"# Hard cases: {name}",
        "",
        f"Recorded {recorded}." + (f" Agent 2 review: `{model}`." if model else " Rules only."),
        "Ground truth is fixed in advance.",
        "",
        "## Outcomes by family",
        "",
    ]
    columns = ["rules (worst-case operator)"] + [f"review, {op} operator" for op in runs]
    lines += ["| Family | " + " | ".join(columns) + " |", "|---|" + "---|" * len(columns)]
    for family in dict.fromkeys(r["family"] for r in rules):
        cells = [_cell(summarize(rules).get(family, {}))]
        for op_rows in runs.values():
            cells.append(_cell(summarize(op_rows).get(family, {})))
        lines.append(f"| {family} | " + " | ".join(cells) + " |")
    lines += ["", "## Review summary", ""]
    for op, op_rows in runs.items():
        s = review_summary(op_rows)
        lines.append(
            f"- {op} operator: {s['reviews']} reviews, {s['answered']} answered, {s['challenged']} challenged, "
            f"{s['unavailable']} unavailable, {s['invalid']} invalid."
        )
    lines += ["", "## Cases", ""]
    header = (
        "| Family | Variant | Truth | Rules: proposed / outcome | "
        + " | ".join(f"Review ({op}): answer / outcome" for op in runs)
        + " |"
    )
    lines += [header, "|---|---|---|---|" + "---|" * len(runs)]
    for index, rule in enumerate(rules):
        cells = [f"{rule['proposed'] or '—'} / {rule['outcome']}"]
        for op_rows in runs.values():
            row = op_rows[index]
            answer = row["review_root_cause"] or row["review_status"]
            cells.append(f"{answer} / {row['outcome']}")
        lines.append(f"| {rule['family']} | {rule['variant']} | {rule['truth'] or '—'} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def _cell(outcomes: dict[str, int]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in outcomes.items()) or "—"


class CachedClient:
    """Replays a model's answer to a request it has already seen. Both operator passes ask the model
    the same questions, so the second pass costs no calls. Failed requests are not cached."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.model_name = inner.model_name
        self._cache: dict[str, Any] = {}
        self.hits = 0

    async def generate(self, *, system, contents, tools):
        key = json.dumps({"system": system, "contents": contents, "tools": tools}, sort_keys=True, default=str)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        reply = await self._inner.generate(system=system, contents=contents, tools=tools)
        self._cache[key] = reply
        return reply

    async def aclose(self) -> None:
        await self._inner.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--llm", choices=["groq", "gemini"], help="run the Agent 2 review on this provider")
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between model-backed cases")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    started = time.time()
    recorded = time.strftime("%Y-%m-%d %H:%M")

    rules_path = OUT / "ambiguous-rules-v1.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))["rows"] if rules_path.exists() else None
    if rules is None:
        rules = asyncio.run(run_all())

    runs: dict[str, list[dict[str, Any]]] = {}
    model = None
    if args.llm:
        if args.llm == "groq":
            key = os.environ.get("GROQ_API_KEY", "")
            if not key:
                raise SystemExit("set GROQ_API_KEY in the environment (never in a file)")
            client = GroqClient(model=args.model, api_key=key, timeout_s=30)
        else:
            key = os.environ.get("GEMINI_API_KEY", "")
            if not key:
                raise SystemExit("set GEMINI_API_KEY in the environment (never in a file)")
            client = GeminiClient(model=args.model, api_key=key, timeout_s=30)
        model = args.model

        cached = CachedClient(client)

        async def both_operators():
            try:
                return {op: await run_all(client=cached, operator=op, pause=args.pause) for op in ("worst", "informed")}
            finally:
                await cached.aclose()

        runs = asyncio.run(both_operators())
        print(f"model answers reused between the two operator passes: {cached.hits}")

    summary = {"rules": summarize(rules), **{f"review-{op}": summarize(r) for op, r in runs.items()}}
    OUT.mkdir(parents=True, exist_ok=True)
    data = {"rules": rules, "runs": runs, "model": model, "summary": summary, "recorded": recorded}
    (OUT / f"{args.name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (OUT / f"{args.name}.md").write_text(render(args.name, recorded, rules, runs, model), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote {OUT / (args.name + '.md')} in {round(time.time() - started, 1)} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
